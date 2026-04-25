import asyncio
import websockets
import json
import random
import socket
import time
import logging

logging.getLogger("websockets").setLevel(logging.ERROR)

# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────
devices           = {}
mobile_clients    = set()
dashboard_clients = set()

device_names     = {}
device_last_seen = {}
device_status    = {}

pair_codes      = {}
paired_devices  = {}
pending_unpairs = set()

IDLE_THRESHOLD    = 10
OFFLINE_THRESHOLD = 25
PAIR_CODE_TTL     = 120

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def now_str():
    return time.strftime("%H:%M:%S")

async def send_log(event, data=None):
    payload = {"type": "server_log", "event": event, "data": data or {}, "time": now_str()}
    msg = json.dumps(payload)
    for ws in list(dashboard_clients):
        try:
            await ws.send(msg)
        except:
            dashboard_clients.discard(ws)

def send_wol(mac: str):
    try:
        print(f"[WOL] Sending magic packet to {mac}")
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        print("[WOL SENT SUCCESS]")
        return True
    except Exception as e:
        print("[WOL ERROR]", e)
        return False

async def broadcast(payload):
    msg = json.dumps(payload)
    for group in [mobile_clients, dashboard_clients]:
        for ws in list(group):
            try:
                await ws.send(msg)
            except:
                group.discard(ws)

async def update_state(device_id):
    now  = time.time()
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
        await send_log("STATE_CHANGE", {
            "device_id": device_id,
            "device_name": device_names.get(device_id, "Unknown-PC"),
            "status": new_status,
            "last_seen": last
        })
        await broadcast({
            "type": "pc_status",
            "device_id": device_id,
            "device_name": device_names.get(device_id, "Unknown-PC"),
            "status": new_status,
            "last_seen": last
        })

async def send_to_device(device_id, payload):
    ws = devices.get(device_id)
    if not ws:
        print("[SEND FAIL] device not connected:", device_id)
        await send_log("COMMAND_FAILED", {"device_id": device_id})
        return False
    try:
        payload["command_id"] = str(random.randint(100000, 999999))
        await ws.send(json.dumps(payload))
        print(f"[SEND] {device_id} → {payload['type']}")
        await send_log("COMMAND_SENT", {"device_id": device_id, "type": payload["type"]})
        return True
    except Exception as e:
        print("[SEND ERROR]", e)
        return False

# ─────────────────────────────────────────────
# Unpair — includes device_name in broadcast so
# the phone can show the actual PC name in alerts
# ─────────────────────────────────────────────
async def do_unpair(device_id: str, notify_mobile: bool = True):
    name = device_names.get(device_id, "Unknown-PC")
    paired_devices.pop(device_id, None)
    pair_codes.pop(device_id, None)

    if device_id in devices:
        await send_to_device(device_id, {"type": "unpaired"})
        print(f"[UNPAIRED] {device_id} — agent notified")
    else:
        pending_unpairs.add(device_id)
        print(f"[UNPAIRED] {device_id} — agent offline, queued")

    await send_log("UNPAIRED", {"device_id": device_id})

    if notify_mobile:
        await broadcast({
            "type": "device_removed",
            "device_id": device_id,
            "device_name": name   # ← phone uses this for the alert message
        })

# ─────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────
async def handler(ws):
    device_id   = None
    client_type = None

    try:
        async for msg in ws:
            print("[RAW MESSAGE]", msg)
            data     = json.loads(msg)
            msg_type = data.get("type")

            await send_log("MESSAGE_RECEIVED", {"type": msg_type})

            # ── PC REGISTRATION ──
            if msg_type == "register":
                device_id   = data["device_id"]
                client_type = "pc"
                devices[device_id]          = ws
                device_last_seen[device_id] = time.time()
                device_names[device_id]     = data.get("device_name", "Unknown-PC")

                agent_is_paired = data.get("is_paired", False)
                if agent_is_paired:
                    paired_devices[device_id] = {"device_name": device_names[device_id]}
                    print(f"[PC CONNECTED] {device_id} ({device_names[device_id]}) — paired")
                else:
                    paired_devices.pop(device_id, None)
                    print(f"[PC CONNECTED] {device_id} ({device_names[device_id]}) — unpaired")

                await send_log("PC_CONNECTED", {
                    "device_id": device_id,
                    "device_name": device_names[device_id],
                    "is_paired": agent_is_paired
                })

                if device_id in pending_unpairs:
                    pending_unpairs.discard(device_id)
                    print(f"[PENDING UNPAIR] Delivering to {device_id}")
                    paired_devices.pop(device_id, None)
                    await send_to_device(device_id, {"type": "unpaired"})
                else:
                    await update_state(device_id)

            # ── HEARTBEAT ──
            elif msg_type == "heartbeat":
                dev = data.get("device_id")
                if dev:
                    device_last_seen[dev] = time.time()
                    await send_log("HEARTBEAT", {"device_id": dev})
                    await update_state(dev)

            # ── MOBILE REGISTRATION ──
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)
                print("[MOBILE CONNECTED]")
                await send_log("MOBILE_CONNECTED")

                for dev_id, status in device_status.items():
                    try:
                        await ws.send(json.dumps({
                            "type": "pc_status",
                            "device_id": dev_id,
                            "device_name": device_names.get(dev_id, "Unknown-PC"),
                            "status": status,
                            "last_seen": device_last_seen.get(dev_id, 0)
                        }))
                    except:
                        pass

            # ── DASHBOARD ──
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)
                print("[DASHBOARD CONNECTED]")
                await send_log("DASHBOARD_CONNECTED")

            # ── PAIRING: AGENT REGISTERS CODE ──
            elif msg_type == "set_pair_code":
                dev_id = data.get("device_id")
                code   = data.get("code")
                if dev_id and code:
                    pair_codes[dev_id] = {
                        "code": code,
                        "expires_at": time.time() + PAIR_CODE_TTL
                    }
                    print(f"[PAIR CODE SET] {dev_id} → {code}")
                    await send_log("PAIR_CODE_SET", {"device_id": dev_id})

            # ── PAIRING: MOBILE SUBMITS CODE ──
            elif msg_type == "pair":
                submitted_code = data.get("code", "").replace(" ", "")

                matched_id = None
                for dev_id, entry in list(pair_codes.items()):
                    if entry["code"] == submitted_code:
                        if time.time() < entry["expires_at"]:
                            matched_id = dev_id
                        else:
                            del pair_codes[dev_id]
                            print(f"[PAIR EXPIRED] {dev_id}")
                        break

                if matched_id:
                    if matched_id in paired_devices:
                        await ws.send(json.dumps({
                            "type": "pair_error",
                            "message": "This PC is already paired to another phone. Please unpair it first from the system tray icon on the PC."
                        }))
                        print(f"[PAIR BLOCKED] {matched_id} already paired")
                    else:
                        del pair_codes[matched_id]
                        paired_devices[matched_id] = {
                            "device_name": device_names.get(matched_id, "Unknown-PC")
                        }
                        print(f"[PAIRED] {matched_id}")
                        await send_log("PAIRED", {"device_id": matched_id})

                        await send_to_device(matched_id, {"type": "pair_confirmed"})

                        await ws.send(json.dumps({
                            "type": "pair_success",
                            "device_id": matched_id,
                            "device_name": device_names.get(matched_id, "Unknown-PC"),
                            "status": device_status.get(matched_id, "offline"),
                            "last_seen": device_last_seen.get(matched_id, 0)
                        }))
                else:
                    print(f"[PAIR FAILED] Invalid/expired: {submitted_code}")
                    await send_log("PAIR_FAILED", {"code": submitted_code})
                    await ws.send(json.dumps({
                        "type": "pair_error",
                        "message": "Invalid or expired code. Please generate a new one on your PC."
                    }))

            # ── UNPAIR: FROM MOBILE ──
            elif msg_type == "unpair_device":
                target_id = data.get("device_id")
                if target_id:
                    await do_unpair(target_id, notify_mobile=False)

            # ── UNPAIR: FROM PC TRAY ──
            elif msg_type == "unpair_from_pc":
                target_id = data.get("device_id")
                if target_id:
                    await do_unpair(target_id, notify_mobile=True)

            # ── COMMANDS ──
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                await send_to_device(data.get("device_id"), {"type": msg_type, "data": {}})

            elif msg_type == "wake_pc":
                target_id = data.get("device_id")
                mac       = data.get("mac")
                print(f"[WAKE COMMAND] device={target_id} mac={mac}")
                await send_log("WOL_TRIGGERED", {"device_id": target_id})
                if mac:
                    success = send_wol(mac)
                else:
                    success = await send_to_device(target_id, {"type": "wake_pc", "data": {}})
                await send_log("WOL_RESULT", {"success": success})

    except Exception as e:
        print("[WS ERROR]", e)
        await send_log("WS_ERROR", {"error": str(e)})

    finally:
        if client_type == "pc" and device_id:
            print(f"[PC DISCONNECTED] {device_id}")
            devices.pop(device_id, None)
            await update_state(device_id)

        if client_type == "mobile":
            mobile_clients.discard(ws)

        if client_type == "dashboard":
            dashboard_clients.discard(ws)

# ─────────────────────────────────────────────
async def state_monitor():
    while True:
        for dev in list(device_last_seen.keys()):
            await update_state(dev)
        await asyncio.sleep(3)

async def main():
    print("[SERVER STARTED] ws://0.0.0.0:8000")
    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(state_monitor())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())