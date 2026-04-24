"""
PC Control Hub — Agent
Runs silently on your Windows PC after first pairing.

Install dependencies:
    pip install websockets qrcode pillow pystray

First run: shows QR popup for pairing.
After pairing: runs silently in system tray.
Tray options: Pair New Device | Unpair Device | Quit
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
import signal
import sys
import tkinter as tk
from tkinter import font as tkfont, messagebox
from io import BytesIO

try:
    import qrcode
    from PIL import Image, ImageTk, ImageDraw
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    print("[WARN] qrcode/pillow not installed. Run: pip install qrcode pillow")

try:
    import pystray
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("[WARN] pystray not installed. Run: pip install pystray")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
SERVER_URL     = "ws://192.168.1.230:8000"
DEVICE_ID_FILE = "device_id.txt"
PAIRED_FILE    = "paired.json"
PAIR_CODE_TTL  = 120

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

DEVICE_ID   = get_device_id()
DEVICE_NAME = get_device_name()

# ─────────────────────────────────────────────
# Paired state
# ─────────────────────────────────────────────
def is_paired():
    if not os.path.exists(PAIRED_FILE):
        return False
    try:
        with open(PAIRED_FILE, "r") as f:
            data = json.load(f)
        return data.get("paired", False)
    except:
        return False

def save_paired(paired: bool):
    with open(PAIRED_FILE, "w") as f:
        json.dump({"paired": paired}, f)

def clear_paired():
    save_paired(False)

# ─────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────
pair_code_ref   = {"code": str(random.randint(100000, 999999))}
popup_ref       = {"root": None}
regenerate_flag = {"pending": False}
unpaired_flag   = {"pending": False}
loop_ref        = {"loop": None}
ws_ref          = {"ws": None}
tray_ref        = {"icon": None}

# ─────────────────────────────────────────────
# PC commands
# ─────────────────────────────────────────────
def shutdown_pc():
    print("[ACTION] Shutdown")
    os.system("shutdown /s /t 0")

def restart_pc():
    print("[ACTION] Restart")
    os.system("shutdown /r /t 0")

def lock_pc():
    print("[ACTION] Lock")
    os.system("rundll32.exe user32.dll,LockWorkStation")

def wake_on_lan(mac: str):
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        print("[WOL] Sent")
    except Exception as e:
        print("[WOL ERROR]", e)

# ─────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────
async def register_pair_code(ws):
    await ws.send(json.dumps({
        "type": "set_pair_code",
        "device_id": DEVICE_ID,
        "code": pair_code_ref["code"]
    }))
    print(f"[PAIR CODE REGISTERED] {pair_code_ref['code']}")

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

async def handle_command(cmd, ws):
    t      = cmd.get("type")
    cmd_id = cmd.get("command_id")
    if not t:
        return
    print(f"[COMMAND] {t}")
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
        elif t == "pair_confirmed":
            save_paired(True)
            print("[PAIRED] Saved to paired.json")
        elif t == "unpaired":
            clear_paired()
            print("[UNPAIRED] Received from server — cleared paired.json")
            unpaired_flag["pending"] = True

        if cmd_id:
            await ws.send(json.dumps({
                "type": "ack",
                "command_id": cmd_id,
                "status": "executed"
            }))
    except Exception as e:
        print("[COMMAND ERROR]", e)

async def connect():
    while True:
        try:
            async with websockets.connect(SERVER_URL) as ws:
                ws_ref["ws"] = ws
                print("[CONNECTED]")

                # ── KEY FIX ──
                # Always tell the server our current paired state so it can
                # correctly set paired_devices without relying on its own
                # in-memory state which resets on server restart.
                await ws.send(json.dumps({
                    "type": "register",
                    "device_id": DEVICE_ID,
                    "device_name": DEVICE_NAME,
                    "is_paired": is_paired()
                }))

                # Only register pair code if not paired — no point showing
                # a code that will be blocked by "already paired" anyway
                if not is_paired():
                    await register_pair_code(ws)

                heartbeat_task = asyncio.create_task(send_heartbeat(ws))

                while True:
                    try:
                        msg  = await ws.recv()
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

def run_async():
    loop = asyncio.new_event_loop()
    loop_ref["loop"] = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(connect())

def reregister_pair_code():
    ws   = ws_ref.get("ws")
    loop = loop_ref.get("loop")
    if ws and loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(register_pair_code(ws), loop)

def send_unpair_to_server():
    ws   = ws_ref.get("ws")
    loop = loop_ref.get("loop")
    if ws and loop and not loop.is_closed():
        async def _send():
            await ws.send(json.dumps({
                "type": "unpair_from_pc",
                "device_id": DEVICE_ID
            }))
        asyncio.run_coroutine_threadsafe(_send(), loop)

def send_register_update():
    """Re-send register with updated is_paired so server state stays in sync."""
    ws   = ws_ref.get("ws")
    loop = loop_ref.get("loop")
    if ws and loop and not loop.is_closed():
        async def _send():
            await ws.send(json.dumps({
                "type": "register",
                "device_id": DEVICE_ID,
                "device_name": DEVICE_NAME,
                "is_paired": is_paired()
            }))
        asyncio.run_coroutine_threadsafe(_send(), loop)

# ─────────────────────────────────────────────
# Tray icon
# ─────────────────────────────────────────────
def make_tray_image():
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill="#22c55e")
    return img

def tray_pair_device(icon, item):
    new_code = str(random.randint(100000, 999999))
    pair_code_ref["code"] = new_code
    reregister_pair_code()
    regenerate_flag["pending"] = True

def tray_unpair_device(icon, item):
    """Triggered from tray: unpair and notify server + mobile."""
    if not is_paired():
        # Show info using a proper root window
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("PC Control Hub", "This PC is not currently paired to any phone.")
        root.destroy()
        return

    # ── KEY FIX ──
    # Always create a hidden root window before calling messagebox.
    # Without this, tkinter has no event loop and the dialog silently fails.
    root = tk.Tk()
    root.withdraw()
    result = messagebox.askyesno(
        "Unpair Device",
        "This will disconnect your phone from this PC.\n\nAre you sure you want to unpair?",
        icon="warning"
    )
    root.destroy()

    if not result:
        return

    clear_paired()
    send_unpair_to_server()
    # Update server's paired_devices immediately
    send_register_update()
    print("[TRAY] Unpaired from PC side")

    root2 = tk.Tk()
    root2.withdraw()
    again = messagebox.askyesno(
        "Pair New Device?",
        "Would you like to pair this PC with a new phone?",
    )
    root2.destroy()

    if again:
        new_code = str(random.randint(100000, 999999))
        pair_code_ref["code"] = new_code
        reregister_pair_code()
        regenerate_flag["pending"] = True

def tray_quit(icon, item):
    icon.stop()
    sys.exit(0)

def start_tray():
    if not TRAY_AVAILABLE:
        return
    menu = pystray.Menu(
        pystray.MenuItem("Pair / Repair Device", tray_pair_device),
        pystray.MenuItem("Unpair Device",         tray_unpair_device),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",                  tray_quit),
    )
    icon = pystray.Icon("PC Control Hub", make_tray_image(), "PC Control Hub", menu)
    tray_ref["icon"] = icon
    icon.run()

# ─────────────────────────────────────────────
# QR popup
# ─────────────────────────────────────────────
def show_pair_popup():
    existing = popup_ref.get("root")
    if existing:
        try:
            existing.destroy()
        except:
            pass

    code = pair_code_ref["code"]

    root = tk.Tk()
    popup_ref["root"] = root
    root.title("PC Control Hub — Pair Device")
    root.configure(bg="#1a1a2e")
    root.resizable(False, False)

    w  = 340
    h  = 480 if QR_AVAILABLE else 280
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    root.attributes("-topmost", True)

    title_font = tkfont.Font(family="Segoe UI", size=13, weight="bold")
    body_font  = tkfont.Font(family="Segoe UI", size=10)
    code_font  = tkfont.Font(family="Courier New", size=30, weight="bold")
    small_font = tkfont.Font(family="Segoe UI", size=8)

    tk.Label(root, text="PC Control Hub", font=title_font, bg="#1a1a2e", fg="#ffffff").pack(pady=(18, 2))
    tk.Label(
        root,
        text="Scan the QR code or enter the manual\ncode in the app to pair your phone.",
        font=body_font, bg="#1a1a2e", fg="#aaaaaa", justify="center"
    ).pack(pady=(0, 10))

    if QR_AVAILABLE:
        qr_data = json.dumps({"server": SERVER_URL, "code": code})
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=3)
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        qr_img.save(buf, format="PNG")
        buf.seek(0)
        pil_img = Image.open(buf).resize((200, 200), Image.NEAREST)
        tk_img  = ImageTk.PhotoImage(pil_img)
        qr_frame = tk.Frame(root, bg="white", padx=4, pady=4)
        qr_frame.pack(pady=(0, 10))
        tk.Label(qr_frame, image=tk_img, bg="white").pack()
        root._qr_img = tk_img

    tk.Label(root, text="Manual Code", font=body_font, bg="#1a1a2e", fg="#aaaaaa").pack()
    code_frame = tk.Frame(root, bg="#0f3460", padx=16, pady=10)
    code_frame.pack(pady=(4, 8))
    spaced = f"{code[:3]} {code[3:]}"
    tk.Label(code_frame, text=spaced, font=code_font, bg="#0f3460", fg="#e94560").pack()

    timer_var = tk.StringVar(value=f"Expires in {PAIR_CODE_TTL}s")
    tk.Label(root, textvariable=timer_var, font=small_font, bg="#1a1a2e", fg="#666688").pack()
    tk.Label(root, text=f"Server: {SERVER_URL}", font=small_font, bg="#1a1a2e", fg="#555577").pack(pady=(4, 0))

    remaining = [PAIR_CODE_TTL]

    def tick():
        remaining[0] -= 1
        if remaining[0] > 0:
            timer_var.set(f"Expires in {remaining[0]}s")
            root.after(1000, tick)
        else:
            new_code = str(random.randint(100000, 999999))
            pair_code_ref["code"] = new_code
            print(f"[AUTO REGENERATE] {new_code}")
            reregister_pair_code()
            root.destroy()
            regenerate_flag["pending"] = True

    root.after(1000, tick)
    root.mainloop()
    popup_ref["root"] = None

# ─────────────────────────────────────────────
# Unpaired dialog
# ─────────────────────────────────────────────
def show_unpaired_dialog():
    root = tk.Tk()
    root.withdraw()
    repair = messagebox.askyesno(
        "PC Control Hub — Device Unpaired",
        "Your phone has been disconnected from this PC.\n\n"
        "Would you like to pair it with a new phone?",
        icon="question"
    )
    root.destroy()

    if repair:
        new_code = str(random.randint(100000, 999999))
        pair_code_ref["code"] = new_code
        reregister_pair_code()
        show_pair_popup()
    else:
        root2 = tk.Tk()
        root2.withdraw()
        uninstall = messagebox.askyesno(
            "PC Control Hub",
            "Would you like to uninstall PC Control Hub from this PC?",
            icon="question"
        )
        root2.destroy()

        if uninstall:
            root3 = tk.Tk()
            root3.withdraw()
            messagebox.showinfo(
                "PC Control Hub",
                "Thank you for using PC Control Hub!\n\n"
                "The application will now close.\n"
                "You can uninstall it from Windows Settings → Apps."
            )
            root3.destroy()
            sys.exit(0)
        else:
            print("[AGENT] Running unpaired in background.")

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[AGENT] Device:    {DEVICE_NAME} ({DEVICE_ID})")
    print(f"[AGENT] Server:    {SERVER_URL}")
    print(f"[AGENT] Paired:    {is_paired()}")

    def handle_sigint(sig, frame):
        print("\n[EXIT] Agent stopped.")
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_sigint)

    bg_thread = threading.Thread(target=run_async, daemon=True)
    bg_thread.start()

    if TRAY_AVAILABLE:
        tray_thread = threading.Thread(target=start_tray, daemon=True)
        tray_thread.start()

    time.sleep(0.8)

    if not is_paired():
        show_pair_popup()

    print("[AGENT] Running. Check system tray for options.")
    while True:
        time.sleep(0.5)

        if regenerate_flag["pending"]:
            regenerate_flag["pending"] = False
            show_pair_popup()

        if unpaired_flag["pending"]:
            unpaired_flag["pending"] = False
            show_unpaired_dialog()