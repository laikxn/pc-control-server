import asyncio
import websockets
import json
import random
import socket
import time
import logging
import os
import secrets

logging.getLogger("websockets").setLevel(logging.ERROR)

# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────
devices           = {}
mobile_clients    = set()
dashboard_clients = set()

device_names     = {}
device_macs      = {}
device_last_seen = {}
device_status    = {}

pair_codes      = {}
paired_devices  = {}
pending_unpairs = set()

device_tokens = {}
pending_acks  = {}

# file_picker: request_id -> origin mobile ws
pending_file_pickers = {}

# scheduled_events: device_id -> list of event dicts
scheduled_events = {}

# queued_notifications: device_id -> list of notification dicts
# Flushed to mobile on next register_mobile
queued_notifications = {}

IDLE_THRESHOLD    = 10
OFFLINE_THRESHOLD = 25
PAIR_CODE_TTL     = 120
TOKENS_FILE       = "tokens.json"
EVENTS_FILE       = "scheduled_events.json"

# ─────────────────────────────────────────────
# Token persistence
# ─────────────────────────────────────────────
def load_tokens():
    global device_tokens
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r") as f:
                device_tokens = json.load(f)
            print(f"[TOKENS] Loaded {len(device_tokens)} token(s)")
        except Exception as e:
            print(f"[TOKENS] Failed to load: {e}"); device_tokens = {}
    else:
        device_tokens = {}

def save_tokens():
    try:
        with open(TOKENS_FILE, "w") as f:
            json.dump(device_tokens, f, indent=2)
    except Exception as e:
        print(f"[TOKENS] Failed to save: {e}")

def generate_token() -> str:
    return secrets.token_hex(32)

def validate_token(device_id: str, token: str) -> bool:
    stored = device_tokens.get(device_id)
    if not stored: return False
    return secrets.compare_digest(stored, token)

# ─────────────────────────────────────────────
# Scheduled events persistence
# ─────────────────────────────────────────────
def load_events():
    global scheduled_events
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r") as f:
                scheduled_events = json.load(f)
            total = sum(len(v) for v in scheduled_events.values())
            print(f"[EVENTS] Loaded {total} event(s)")
        except Exception as e:
            print(f"[EVENTS] Failed to load: {e}"); scheduled_events = {}
    else:
        scheduled_events = {}

def save_events():
    try:
        with open(EVENTS_FILE, "w") as f:
            json.dump(scheduled_events, f, indent=2)
    except Exception as e:
        print(f"[EVENTS] Failed to save: {e}")

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def now_str():
    return time.strftime("%H:%M:%S")

async def send_log(event, data=None):
    payload = {"type": "server_log", "event": event, "data": data or {}, "time": now_str()}
    msg = json.dumps(payload)
    for ws in list(dashboard_clients):
        try:    await ws.send(msg)
        except: dashboard_clients.discard(ws)

def send_wol(mac: str):
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet    = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        print("[WOL SENT]")
        return True
    except Exception as e:
        print("[WOL ERROR]", e); return False

async def broadcast(payload):
    msg = json.dumps(payload)
    for group in [mobile_clients, dashboard_clients]:
        for ws in list(group):
            try:    await ws.send(msg)
            except: group.discard(ws)

async def broadcast_to_mobile(payload):
    msg = json.dumps(payload)
    for ws in list(mobile_clients):
        try:    await ws.send(msg)
        except: mobile_clients.discard(ws)

async def update_state(device_id):
    now  = time.time()
    last = device_last_seen.get(device_id, 0)
    if device_id not in devices:
        new_status = "offline"
    else:
        diff = now - last
        if diff > OFFLINE_THRESHOLD:   new_status = "offline"
        elif diff > IDLE_THRESHOLD:    new_status = "idle"
        else:                          new_status = "online"
    old = device_status.get(device_id)
    if old != new_status:
        device_status[device_id] = new_status
        print(f"[STATE] {device_id} → {new_status}")
        await send_log("STATE_CHANGE", {"device_id": device_id, "status": new_status})
        await broadcast({
            "type": "pc_status", "device_id": device_id,
            "device_name": device_names.get(device_id, "Unknown-PC"),
            "device_mac":  device_macs.get(device_id, ""),
            "status":      new_status, "last_seen": last,
        })

async def send_to_device(device_id, payload, origin_ws=None):
    ws = devices.get(device_id)
    if not ws:
        print("[SEND FAIL] device not connected:", device_id)
        return False
    try:
        cmd_id = str(random.randint(100000, 999999))
        payload["command_id"] = cmd_id
        if origin_ws is not None:
            pending_acks[cmd_id] = origin_ws
        await ws.send(json.dumps(payload))
        print(f"[SEND] {device_id} → {payload['type']} (cmd {cmd_id})")
        return True
    except Exception as e:
        print("[SEND ERROR]", e); return False

async def do_unpair(device_id: str, notify_mobile: bool = True):
    name = device_names.get(device_id, "Unknown-PC")
    paired_devices.pop(device_id, None)
    pair_codes.pop(device_id, None)
    device_tokens.pop(device_id, None)
    save_tokens()
    scheduled_events.pop(device_id, None)
    save_events()

    if device_id in devices:
        await send_to_device(device_id, {"type": "unpaired"})
    else:
        pending_unpairs.add(device_id)

    await send_log("UNPAIRED", {"device_id": device_id})
    if notify_mobile:
        await broadcast({"type": "device_removed", "device_id": device_id, "device_name": name})

async def reject_token(ws, device_id: str):
    try:
        await ws.send(json.dumps({
            "type": "token_invalid", "device_id": device_id,
            "message": "Invalid token. Please re-pair this device.",
        }))
    except: pass

# ─────────────────────────────────────────────
# Queued notifications — flushed when mobile connects
# ─────────────────────────────────────────────
def queue_notification(device_id: str, notif: dict):
    if device_id not in queued_notifications:
        queued_notifications[device_id] = []
    queued_notifications[device_id].append({**notif, "device_id": device_id})

async def flush_notifications(ws):
    all_notifs = []
    for notifs in queued_notifications.values():
        all_notifs.extend(notifs)
    if all_notifs:
        queued_notifications.clear()
        try:
            await ws.send(json.dumps({"type": "queued_notifications", "notifications": all_notifs}))
        except: pass

# ─────────────────────────────────────────────
# Scheduled event execution
# ─────────────────────────────────────────────
def should_event_fire(event: dict, now_ts: float) -> bool:
    if not event.get("enabled", True):
        return False
    import datetime
    now_dt  = datetime.datetime.fromtimestamp(now_ts)
    if now_dt.hour   != event.get("hour",   -1): return False
    if now_dt.minute != event.get("minute", -1): return False
    recurrence = event.get("recurrence", "once")
    if recurrence == "once":
        return not event.get("fired", False)
    last_fired = event.get("last_fired", 0)
    if now_ts - last_fired < 60:
        return False
    if recurrence == "daily":
        return True
    if recurrence == "weekly":
        return now_dt.weekday() in event.get("days", [])
    return False

async def execute_scheduled_event(device_id: str, event: dict):
    steps     = event.get("steps", [])
    event_id  = event.get("id", "")
    name      = event.get("name", "Scheduled Event")
    if not steps:
        return

    print(f"[EVENT] Firing '{name}' for {device_id}")
    first_step    = steps[0] if steps else {}
    wake_is_first = first_step.get("type") == "wake_pc"
    pc_online     = device_status.get(device_id) in ("online", "idle")

    if wake_is_first:
        # Send WoL
        mac = device_macs.get(device_id, "")
        if mac:
            send_wol(mac)
        else:
            await send_to_device(device_id, {"type": "wake_pc"})

        # Queue remaining steps on agent for post-boot execution
        remaining = steps[1:]
        if remaining:
            agent_steps = []
            for step in remaining:
                stype = step.get("type")
                if stype == "run_custom_action":
                    agent_steps.append({"type": "run_file", "path": step.get("path", "")})
                elif stype in ("shutdown_pc", "restart_pc", "lock_pc"):
                    agent_steps.append({"type": stype})
            if agent_steps:
                await send_to_device(device_id, {"type": "save_startup_queue", "steps": agent_steps})

        await broadcast_to_mobile({"type": "event_fired", "device_id": device_id, "event_id": event_id, "event_name": name})

    elif not pc_online:
        # PC offline, no wake step — can't run. Queue notification.
        print(f"[EVENT] '{name}' skipped — {device_id} is offline")
        notif = {"type": "event_failed", "event_id": event_id, "event_name": name,
                 "reason": "offline", "timestamp": time.time()}
        queue_notification(device_id, notif)
        await broadcast_to_mobile({"type": "event_failed", "device_id": device_id,
                                   "event_id": event_id, "event_name": name, "reason": "offline"})
    else:
        # PC online — run steps sequentially
        for step in steps:
            stype = step.get("type")
            if stype == "run_custom_action":
                await send_to_device(device_id, {"type": "run_custom_action", "path": step.get("path", "")})
                await asyncio.sleep(1)
            elif stype in ("shutdown_pc", "restart_pc", "lock_pc"):
                await send_to_device(device_id, {"type": stype})
                await asyncio.sleep(1)
            elif stype == "wake_pc":
                mac = device_macs.get(device_id, "")
                if mac: send_wol(mac)
                else:   await send_to_device(device_id, {"type": "wake_pc"})
                await asyncio.sleep(2)
        await broadcast_to_mobile({"type": "event_fired", "device_id": device_id, "event_id": event_id, "event_name": name})

async def scheduler_loop():
    """Checks all scheduled events every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        now_ts = time.time()
        for device_id, events in list(scheduled_events.items()):
            for event in events:
                if should_event_fire(event, now_ts):
                    event["last_fired"] = now_ts
                    if event.get("recurrence", "once") == "once":
                        event["fired"] = True
                    save_events()
                    asyncio.create_task(execute_scheduled_event(device_id, event))

# ─────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────
async def handler(ws):
    device_id   = None
    client_type = None

    try:
        async for msg in ws:
            print("[RAW]", msg[:120])
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
                mac = data.get("device_mac", "")
                if mac: device_macs[device_id] = mac
                agent_is_paired = data.get("is_paired", False)
                if agent_is_paired:
                    paired_devices[device_id] = {"device_name": device_names[device_id]}
                else:
                    paired_devices.pop(device_id, None)
                print(f"[PC CONNECTED] {device_id} ({'paired' if agent_is_paired else 'unpaired'})")
                await send_log("PC_CONNECTED", {"device_id": device_id, "is_paired": agent_is_paired})
                if device_id in pending_unpairs:
                    pending_unpairs.discard(device_id)
                    paired_devices.pop(device_id, None)
                    await send_to_device(device_id, {"type": "unpaired"})
                else:
                    await update_state(device_id)

            # ── HEARTBEAT ──
            elif msg_type == "heartbeat":
                dev = data.get("device_id")
                if dev:
                    device_last_seen[dev] = time.time()
                    await update_state(dev)

            # ── PC STATS ──
            elif msg_type == "pc_stats":
                dev_id = data.get("device_id")
                if dev_id: await broadcast_to_mobile(data)

            # ── ACK FROM AGENT ──
            elif msg_type == "ack":
                cmd_id = data.get("command_id"); ack_status = data.get("status", "executed")
                if cmd_id and cmd_id in pending_acks:
                    origin_ws = pending_acks.pop(cmd_id)
                    try:
                        await origin_ws.send(json.dumps({"type": "command_ack", "command_id": cmd_id, "status": ack_status}))
                    except: pass

            # ── FILE PICKER RESULT FROM AGENT ──
            elif msg_type == "file_picker_result":
                request_id = data.get("request_id"); path = data.get("path")
                print(f"[FILE PICKER RESULT] req={request_id} path={path}")
                if request_id and request_id in pending_file_pickers:
                    origin_ws = pending_file_pickers.pop(request_id)
                    try:
                        await origin_ws.send(json.dumps({"type": "file_picker_result", "request_id": request_id, "path": path}))
                    except: pass

            # ── MOBILE REGISTRATION ──
            elif msg_type == "register_mobile":
                client_type = "mobile"
                mobile_clients.add(ws)
                print("[MOBILE CONNECTED]")
                for dev_id, status in device_status.items():
                    try:
                        await ws.send(json.dumps({
                            "type": "pc_status", "device_id": dev_id,
                            "device_name": device_names.get(dev_id, "Unknown-PC"),
                            "device_mac":  device_macs.get(dev_id, ""),
                            "status":      status, "last_seen": device_last_seen.get(dev_id, 0),
                        }))
                    except: pass
                await flush_notifications(ws)

            # ── DASHBOARD ──
            elif msg_type == "register_dashboard":
                client_type = "dashboard"
                dashboard_clients.add(ws)

            # ── PAIR CODE SET BY AGENT ──
            elif msg_type == "set_pair_code":
                dev_id = data.get("device_id"); code = data.get("code")
                if dev_id and code:
                    pair_codes[dev_id] = {"code": code, "expires_at": time.time() + PAIR_CODE_TTL}
                    print(f"[PAIR CODE SET] {dev_id} → {code}")

            # ── PAIR SUBMITTED BY MOBILE ──
            elif msg_type == "pair":
                submitted_code = data.get("code", "").replace(" ", "")
                matched_id = None
                for dev_id, entry in list(pair_codes.items()):
                    if entry["code"] == submitted_code:
                        if time.time() < entry["expires_at"]: matched_id = dev_id
                        else: del pair_codes[dev_id]
                        break
                if matched_id:
                    if matched_id in paired_devices:
                        await ws.send(json.dumps({"type": "pair_error", "message": "This PC is already paired. Please unpair it first from the system tray."}))
                    else:
                        del pair_codes[matched_id]
                        paired_devices[matched_id] = {"device_name": device_names.get(matched_id, "Unknown-PC")}
                        token = generate_token()
                        device_tokens[matched_id] = token
                        save_tokens()
                        await send_to_device(matched_id, {"type": "pair_confirmed"})
                        await ws.send(json.dumps({
                            "type": "pair_success", "device_id": matched_id,
                            "device_name":  device_names.get(matched_id, "Unknown-PC"),
                            "device_mac":   device_macs.get(matched_id, ""),
                            "device_token": token,
                            "status":       device_status.get(matched_id, "offline"),
                            "last_seen":    device_last_seen.get(matched_id, 0),
                        }))
                else:
                    await ws.send(json.dumps({"type": "pair_error", "message": "Invalid or expired code. Please generate a new one on your PC."}))

            # ── UNPAIR FROM MOBILE ──
            elif msg_type == "unpair_device":
                target_id = data.get("device_id"); token = data.get("token", "")
                if target_id:
                    if target_id in device_tokens and not validate_token(target_id, token):
                        await reject_token(ws, target_id)
                    else:
                        await do_unpair(target_id, notify_mobile=False)

            # ── UNPAIR FROM PC TRAY ──
            elif msg_type == "unpair_from_pc":
                target_id = data.get("device_id")
                if target_id: await do_unpair(target_id, notify_mobile=True)

            # ── STANDARD COMMANDS ──
            elif msg_type in ["shutdown_pc", "restart_pc", "lock_pc"]:
                target_id = data.get("device_id"); token = data.get("token", "")
                if not target_id: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                await send_to_device(target_id, {"type": msg_type}, origin_ws=ws)

            elif msg_type == "wake_pc":
                target_id = data.get("device_id"); token = data.get("token", "")
                if not target_id: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                mac = data.get("mac") or device_macs.get(target_id, "")
                if mac:
                    success = send_wol(mac)
                    try:
                        await ws.send(json.dumps({"type": "command_ack", "status": "executed" if success else "failed", "is_wol": True}))
                    except: pass
                else:
                    await send_to_device(target_id, {"type": "wake_pc"}, origin_ws=ws)

            # ── CUSTOM ACTION ──
            elif msg_type == "run_custom_action":
                target_id = data.get("device_id"); token = data.get("token", ""); path = data.get("path", "")
                if not target_id or not path: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                await send_to_device(target_id, {"type": "run_custom_action", "path": path}, origin_ws=ws)

            # ── FILE PICKER REQUEST FROM MOBILE ──
            elif msg_type == "open_file_picker":
                target_id  = data.get("device_id"); token = data.get("token", "")
                request_id = data.get("request_id", str(random.randint(100000, 999999)))
                if not target_id: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                if target_id not in devices:
                    try:
                        await ws.send(json.dumps({"type": "file_picker_result", "request_id": request_id, "path": None, "error": "PC is offline"}))
                    except: pass
                    continue
                pending_file_pickers[request_id] = ws
                await send_to_device(target_id, {"type": "open_file_picker", "request_id": request_id})

            # ── SAVE EVENTS ──
            elif msg_type == "save_events":
                target_id = data.get("device_id"); token = data.get("token", ""); events = data.get("events", [])
                if not target_id: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                scheduled_events[target_id] = events
                save_events()
                print(f"[EVENTS] Saved {len(events)} event(s) for {target_id}")
                try: await ws.send(json.dumps({"type": "events_saved", "device_id": target_id}))
                except: pass

            # ── GET EVENTS ──
            elif msg_type == "get_events":
                target_id = data.get("device_id"); token = data.get("token", "")
                if not target_id: continue
                if target_id in device_tokens and not validate_token(target_id, token):
                    await reject_token(ws, target_id); continue
                try:
                    await ws.send(json.dumps({"type": "events_data", "device_id": target_id, "events": scheduled_events.get(target_id, [])}))
                except: pass

    except Exception as e:
        print("[WS ERROR]", e)
    finally:
        if client_type == "pc" and device_id:
            print(f"[PC DISCONNECTED] {device_id}")
            devices.pop(device_id, None)
            await update_state(device_id)
        if client_type == "mobile":    mobile_clients.discard(ws)
        if client_type == "dashboard": dashboard_clients.discard(ws)

# ─────────────────────────────────────────────
async def state_monitor():
    while True:
        for dev in list(device_last_seen.keys()):
            await update_state(dev)
        await asyncio.sleep(3)

async def main():
    load_tokens()
    load_events()
    print("[SERVER STARTED] ws://0.0.0.0:8000")
    async with websockets.serve(handler, "0.0.0.0", 8000):
        asyncio.create_task(state_monitor())
        asyncio.create_task(scheduler_loop())
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())