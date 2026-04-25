"""
PC Control Hub — Agent
Runs silently on your Windows PC after first pairing.

Install dependencies:
    pip install websockets qrcode pillow pystray

First run: shows QR popup for pairing.
After pairing: runs silently in system tray.
Tray right-click: Pair / Repair Device | Unpair Device | Quit

IMPORTANT: All tkinter UI must run on the main thread.
Tray callbacks only set flags. Main loop handles all UI.
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
from tkinter import font as tkfont
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
# Flags — tray callbacks only set these.
# Main loop reads them and does all UI work.
#
# we_initiated_unpair: set when WE sent unpair_from_pc to the server.
# Prevents the echo "unpaired" message from triggering a second dialog.
# ─────────────────────────────────────────────
flags = {
    "show_qr":              False,
    "close_popup":          False,
    "show_unpaired":        False,  # set by server echo — only if we didn't initiate
    "tray_unpair":          False,
    "tray_quit":            False,
    "we_initiated_unpair":  False,  # suppresses echo from server after tray unpair
}

loop_ref      = {"loop": None}
ws_ref        = {"ws": None}
tray_ref      = {"icon": None}
popup_ref     = {"root": None}
pair_code_ref = {"code": ""}  # always generated fresh before showing popup

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
# WebSocket helpers
# ─────────────────────────────────────────────
async def _ws_send(payload: dict):
    ws = ws_ref.get("ws")
    if ws:
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            print("[WS SEND ERROR]", e)

def threadsafe_send(payload: dict):
    loop = loop_ref.get("loop")
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_ws_send(payload), loop)

async def _register_and_code(ws):
    """Send register + fresh pair code atomically."""
    await ws.send(json.dumps({
        "type": "register",
        "device_id": DEVICE_ID,
        "device_name": DEVICE_NAME,
        "is_paired": is_paired()
    }))
    if not is_paired():
        await ws.send(json.dumps({
            "type": "set_pair_code",
            "device_id": DEVICE_ID,
            "code": pair_code_ref["code"]
        }))
        print(f"[PAIR CODE REGISTERED] {pair_code_ref['code']}")

def threadsafe_register_and_code():
    ws   = ws_ref.get("ws")
    loop = loop_ref.get("loop")
    if ws and loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_register_and_code(ws), loop)

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
        if t == "shutdown_pc":   shutdown_pc()
        elif t == "restart_pc":  restart_pc()
        elif t == "lock_pc":     lock_pc()
        elif t == "wake_pc":
            mac = cmd.get("mac")
            if mac: wake_on_lan(mac)
        elif t == "reload_agent": os._exit(0)

        elif t == "pair_confirmed":
            save_paired(True)
            print("[PAIRED] Saved to paired.json")
            flags["close_popup"] = True

        elif t == "unpaired":
            clear_paired()
            print("[UNPAIRED] Received from server")
            # Only show the dialog if the phone removed us.
            # If WE initiated the unpair from the tray, suppress this echo.
            if flags["we_initiated_unpair"]:
                print("[UNPAIRED] Suppressing echo — we initiated this unpair")
                flags["we_initiated_unpair"] = False
            else:
                flags["show_unpaired"] = True

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

                await ws.send(json.dumps({
                    "type": "register",
                    "device_id": DEVICE_ID,
                    "device_name": DEVICE_NAME,
                    "is_paired": is_paired()
                }))

                if not is_paired() and pair_code_ref["code"]:
                    await ws.send(json.dumps({
                        "type": "set_pair_code",
                        "device_id": DEVICE_ID,
                        "code": pair_code_ref["code"]
                    }))
                    print(f"[PAIR CODE REGISTERED] {pair_code_ref['code']}")

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

# ─────────────────────────────────────────────
# Tray — callbacks only set flags, no UI here
# ─────────────────────────────────────────────
def tray_on_pair(icon, item):
    flags["show_qr"] = True

def tray_on_unpair(icon, item):
    flags["tray_unpair"] = True

def tray_on_quit(icon, item):
    flags["tray_quit"] = True

def make_tray_image():
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill="#22c55e")
    return img

def start_tray():
    if not TRAY_AVAILABLE:
        return
    menu = pystray.Menu(
        pystray.MenuItem("Pair / Repair Device", tray_on_pair),
        pystray.MenuItem("Unpair Device",         tray_on_unpair),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",                  tray_on_quit),
    )
    icon = pystray.Icon("PC Control Hub", make_tray_image(), "PC Control Hub", menu)
    tray_ref["icon"] = icon
    icon.run()

# ─────────────────────────────────────────────
# Custom topmost dialog — guaranteed above all windows
# ─────────────────────────────────────────────
def _topmost_dialog(title: str, message: str, kind: str = "yesno"):
    """
    kind: "yesno" → True/False
          "info"  → None
    """
    result = [None]

    dlg = tk.Tk()
    dlg.title(title)
    dlg.configure(bg="#1a1a2e")
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)
    dlg.lift()
    dlg.focus_force()

    w = 380
    h = 160 if kind == "info" else 190
    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    body_font = tkfont.Font(family="Segoe UI", size=10)
    btn_font  = tkfont.Font(family="Segoe UI", size=10, weight="bold")

    tk.Label(dlg, text=message, font=body_font, bg="#1a1a2e", fg="#cccccc",
             wraplength=340, justify="center").pack(pady=(24, 16), padx=20)

    btn_frame = tk.Frame(dlg, bg="#1a1a2e")
    btn_frame.pack(pady=(0, 16))

    if kind == "yesno":
        def on_yes(): result[0] = True;  dlg.destroy()
        def on_no():  result[0] = False; dlg.destroy()
        tk.Button(btn_frame, text="Yes", font=btn_font, bg="#22c55e", fg="white",
                  relief="flat", padx=22, pady=6, command=on_yes).pack(side="left", padx=8)
        tk.Button(btn_frame, text="No",  font=btn_font, bg="#333355", fg="white",
                  relief="flat", padx=22, pady=6, command=on_no).pack(side="left", padx=8)
    else:
        tk.Button(btn_frame, text="OK", font=btn_font, bg="#007aff", fg="white",
                  relief="flat", padx=28, pady=6, command=dlg.destroy).pack()

    dlg.mainloop()
    return result[0]

# ─────────────────────────────────────────────
# Main-thread UI
# ─────────────────────────────────────────────
def close_popup_if_open():
    root = popup_ref.get("root")
    if root:
        try:
            root.destroy()
        except:
            pass
        popup_ref["root"] = None

def _fresh_code() -> str:
    """Generate a new pair code, store it, and return it."""
    code = str(random.randint(100000, 999999))
    pair_code_ref["code"] = code
    return code

def show_pair_popup():
    """
    Always generates a brand-new code before showing the popup.
    Registers it with the server, then opens the window.
    Main thread only.
    """
    close_popup_if_open()

    # Always fresh code — never reuse the last one
    code = _fresh_code()
    print(f"[NEW PAIR CODE] {code}")
    threadsafe_register_and_code()
    # Give server a moment to store the code before the popup appears
    time.sleep(0.4)

    root = tk.Tk()
    popup_ref["root"] = root
    root.title("PC Control Hub — Pair Device")
    root.configure(bg="#1a1a2e")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    w  = 340
    h  = 480 if QR_AVAILABLE else 280
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

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
        # Close silently if pairing succeeded while popup was open
        if is_paired():
            root.destroy()
            return
        remaining[0] -= 1
        if remaining[0] > 0:
            timer_var.set(f"Expires in {remaining[0]}s")
            root.after(1000, tick)
        else:
            if not is_paired():
                # Auto-regenerate — set flag so main loop reopens with fresh code
                print("[AUTO REGENERATE] Timer expired")
                root.destroy()
                flags["show_qr"] = True
            else:
                root.destroy()

    root.after(1000, tick)
    root.mainloop()
    popup_ref["root"] = None

def handle_show_qr():
    """
    Triggered by flags["show_qr"].
    If already paired: show info dialog to unpair first.
    If not paired: show QR popup with a fresh code.
    """
    if is_paired():
        _topmost_dialog(
            "PC Control Hub — Already Paired",
            f"{DEVICE_NAME} is already paired to a phone.\n\n"
            "To pair with a different phone, select\n"
            "\"Unpair Device\" from the tray icon first.",
            kind="info"
        )
        return
    show_pair_popup()

def handle_unpaired_dialog():
    """
    Phone removed us. Offer re-pair or uninstall.
    Called only when the phone initiated the unpair (not us).
    """
    repair = _topmost_dialog(
        "PC Control Hub — Device Unpaired",
        "Your phone has been disconnected from this PC.\n\n"
        "Would you like to pair with a new phone?",
        kind="yesno"
    )

    if repair:
        show_pair_popup()
    else:
        uninstall = _topmost_dialog(
            "PC Control Hub",
            "Would you like to uninstall PC Control Hub from this PC?",
            kind="yesno"
        )
        if uninstall:
            _topmost_dialog(
                "PC Control Hub",
                "Thank you for using PC Control Hub!\n\n"
                "The application will now close.\n"
                "You can uninstall it from Windows Settings → Apps.",
                kind="info"
            )
            sys.exit(0)
        else:
            print("[AGENT] Running unpaired in background.")

def handle_tray_unpair():
    """Tray 'Unpair Device' clicked."""
    if not is_paired():
        _topmost_dialog(
            "PC Control Hub",
            "This PC is not currently paired to any phone.",
            kind="info"
        )
        return

    result = _topmost_dialog(
        "Unpair Device",
        f"This will disconnect your phone from {DEVICE_NAME}.\n\n"
        "Are you sure you want to unpair?",
        kind="yesno"
    )

    if not result:
        return

    # Set flag BEFORE sending so handle_command can suppress the echo
    flags["we_initiated_unpair"] = True
    clear_paired()
    threadsafe_send({"type": "unpair_from_pc", "device_id": DEVICE_ID})
    # Re-register so server updates paired_devices immediately
    threadsafe_send({
        "type": "register",
        "device_id": DEVICE_ID,
        "device_name": DEVICE_NAME,
        "is_paired": False
    })
    print("[TRAY] Unpaired from PC side")

    again = _topmost_dialog(
        "Pair New Device?",
        "Would you like to pair this PC with a new phone?",
        kind="yesno"
    )

    if again:
        show_pair_popup()

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

    # First run — generate code and show popup if not paired
    if not is_paired():
        _fresh_code()
        show_pair_popup()

    print("[AGENT] Running. Right-click the tray icon for options.")
    while True:
        time.sleep(0.4)

        if flags["tray_quit"]:
            flags["tray_quit"] = False
            print("[EXIT] Quit from tray.")
            sys.exit(0)

        if flags["close_popup"]:
            flags["close_popup"] = False
            close_popup_if_open()

        if flags["show_qr"]:
            flags["show_qr"] = False
            handle_show_qr()

        if flags["show_unpaired"]:
            flags["show_unpaired"] = False
            handle_unpaired_dialog()

        if flags["tray_unpair"]:
            flags["tray_unpair"] = False
            handle_tray_unpair()