"""
PC Control Hub — Agent
Run this on your Windows PC. It will:
  1. Generate a 6-digit pairing code
  2. Show a small popup with the QR code + plain code
  3. Connect to the server in the background
  4. Listen for and execute commands (shutdown, restart, lock, wake)

Install dependencies before first run:
    pip install websockets qrcode pillow
"""

import asyncio
import websockets
import json
import uuid
import time
import os
import socket
import threading
import random
import tkinter as tk
from tkinter import font as tkfont
from io import BytesIO

try:
    import qrcode
    from PIL import Image, ImageTk
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    print("[WARN] qrcode/pillow not installed. Run: pip install qrcode pillow")
    print("[WARN] Pairing popup will show text code only.")

# ─────────────────────────────────────────────
# Config — change SERVER_URL when moving to cloud
# ─────────────────────────────────────────────
SERVER_URL = "ws://192.168.1.230:8000"
DEVICE_ID_FILE = "device_id.txt"
PAIR_CODE_TTL = 120  # seconds

# ─────────────────────────────────────────────
# Device identity
# ─────────────────────────────────────────────
def get_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE, "r") as f:
            return f.read().strip()
    device_id = str(uuid.uuid4())
    with open(DEVICE_ID_FILE, "w") as f:
        f.write(device_id)
    return device_id

def get_device_name():
    return os.environ.get("COMPUTERNAME", socket.gethostname())

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

DEVICE_ID = get_device_id()
DEVICE_NAME = get_device_name()
LOCAL_IP = get_local_ip()

# ─────────────────────────────────────────────
# Pairing code
# ─────────────────────────────────────────────
def gen_pair_code():
    return str(random.randint(100000, 999999))

PAIR_CODE = gen_pair_code()
pair_code_expiry = time.time() + PAIR_CODE_TTL

# Shared websocket ref so the popup can send the code to the server
ws_ref = {"ws": None}

# ─────────────────────────────────────────────
# PC commands
# ─────────────────────────────────────────────
def shutdown_pc():
    print("[ACTION] Shutdown triggered")
    os.system("shutdown /s /t 0")

def restart_pc():
    print("[ACTION] Restart triggered")
    os.system("shutdown /r /t 0")

def lock_pc():
    print("[ACTION] Lock triggered")
    os.system("rundll32.exe user32.dll,LockWorkStation")

def wake_on_lan(mac: str):
    try:
        print(f"[WOL] Sending magic packet to {mac}")
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        print("[WOL] Sent successfully")
    except Exception as e:
        print("[WOL ERROR]", e)

# ─────────────────────────────────────────────
# Pairing popup (tkinter, runs on main thread)
# ─────────────────────────────────────────────
def show_pair_popup(code: str, ip: str, port: int = 8000):
    """Show a small window with the pairing QR code and manual code."""

    root = tk.Tk()
    root.title("PC Control Hub — Pair Device")
    root.configure(bg="#1a1a2e")
    root.resizable(False, False)

    # Center on screen
    w, h = 340, 480 if QR_AVAILABLE else 280
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # Keep window on top
    root.attributes("-topmost", True)

    title_font = tkfont.Font(family="Segoe UI", size=13, weight="bold")
    body_font  = tkfont.Font(family="Segoe UI", size=10)
    code_font  = tkfont.Font(family="Courier New", size=28, weight="bold")
    small_font = tkfont.Font(family="Segoe UI", size=8)

    tk.Label(
        root,
        text="PC Control Hub",
        font=title_font,
        bg="#1a1a2e",
        fg="#ffffff"
    ).pack(pady=(18, 2))

    tk.Label(
        root,
        text="Scan this QR code or enter the\nmanual code in the app to pair your phone.",
        font=body_font,
        bg="#1a1a2e",
        fg="#aaaaaa",
        justify="center"
    ).pack(pady=(0, 10))

    # QR code image
    if QR_AVAILABLE:
        qr_data = json.dumps({"ip": ip, "port": port, "code": code})
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=3
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # Convert to tkinter-compatible image
        buf = BytesIO()
        qr_img.save(buf, format="PNG")
        buf.seek(0)
        pil_img = Image.open(buf).resize((200, 200), Image.NEAREST)
        tk_img = ImageTk.PhotoImage(pil_img)

        qr_frame = tk.Frame(root, bg="white", padx=4, pady=4)
        qr_frame.pack(pady=(0, 10))
        tk.Label(qr_frame, image=tk_img, bg="white").pack()
        root._qr_img = tk_img  # prevent garbage collection

    # Manual code display
    tk.Label(
        root,
        text="Manual Code",
        font=body_font,
        bg="#1a1a2e",
        fg="#aaaaaa"
    ).pack()

    code_frame = tk.Frame(root, bg="#0f3460", padx=16, pady=10)
    code_frame.pack(pady=(4, 8))
    tk.Label(
        code_frame,
        text=code,
        font=code_font,
        bg="#0f3460",
        fg="#e94560",
        letter_spacing=8
    ).pack()

    # Timer label
    timer_var = tk.StringVar(value=f"Expires in {PAIR_CODE_TTL}s")
    tk.Label(
        root,
        textvariable=timer_var,
        font=small_font,
        bg="#1a1a2e",
        fg="#666688"
    ).pack()

    # IP info
    tk.Label(
        root,
        text=f"Your PC IP: {ip}:{port}",
        font=small_font,
        bg="#1a1a2e",
        fg="#555577"
    ).pack(pady=(4, 0))

    # Close button
    tk.Button(
        root,
        text="Close",
        font=body_font,
        bg="#333355",
        fg="white",
        relief="flat",
        padx=20,
        pady=6,
        command=root.destroy
    ).pack(pady=(12, 16))

    # Countdown timer
    remaining = [PAIR_CODE_TTL]

    def tick():
        remaining[0] -= 1
        if remaining[0] > 0:
            timer_var.set(f"Expires in {remaining[0]}s")
            root.after(1000, tick)
        else:
            timer_var.set("Code expired — restart app to generate new code")

    root.after(1000, tick)
    root.mainloop()


# ─────────────────────────────────────────────
# WebSocket: send the code to the server
# ─────────────────────────────────────────────
async def register_pair_code(ws):
    """Tell the server about our pairing code so it can validate mobile requests."""
    await ws.send(json.dumps({
        "type": "set_pair_code",
        "device_id": DEVICE_ID,
        "code": PAIR_CODE
    }))
    print(f"[PAIR CODE REGISTERED] {PAIR_CODE} (valid {PAIR_CODE_TTL}s)")


# ─────────────────────────────────────────────
# WebSocket: heartbeat
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# WebSocket: command handler
# ─────────────────────────────────────────────
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
            mac = cmd.get("mac")
            if mac:
                wake_on_lan(mac)
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


# ─────────────────────────────────────────────
# Main connection loop
# ─────────────────────────────────────────────
async def connect():
    while True:
        try:
            async with websockets.connect(SERVER_URL) as ws:
                ws_ref["ws"] = ws
                print("[CONNECTED]")

                await ws.send(json.dumps({
                    "type": "register",
                    "device_id": DEVICE_ID,
                    "device_name": DEVICE_NAME
                }))

                # Register our pairing code with the server
                await register_pair_code(ws)

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

        ws_ref["ws"] = None
        await asyncio.sleep(3)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
def run_async():
    """Run the asyncio event loop in a background thread."""
    asyncio.run(connect())


if __name__ == "__main__":
    print(f"[AGENT] Device: {DEVICE_NAME} ({DEVICE_ID})")
    print(f"[AGENT] Local IP: {LOCAL_IP}")
    print(f"[AGENT] Pair code: {PAIR_CODE}")

    # Start WebSocket connection in background thread
    bg_thread = threading.Thread(target=run_async, daemon=True)
    bg_thread.start()

    # Show pairing popup on the main thread (tkinter requirement)
    show_pair_popup(PAIR_CODE, LOCAL_IP)

    # After popup is closed, keep running silently in the background
    print("[AGENT] Running in background. Close this window to stop.")

    # Keep main thread alive so daemon thread keeps running
    try:
        bg_thread.join()
    except KeyboardInterrupt:
        print("\n[EXIT] Agent stopped.")