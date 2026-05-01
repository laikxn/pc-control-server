"""
PCLink — Agent
Runs silently on your Windows PC after first pairing.

Install dependencies:
    pip install websockets qrcode pillow pystray psutil
    pip install GPUtil  (optional, Nvidia GPU stats)
    pip install pycaw   (optional, Windows volume mixer)

First run: shows QR popup for pairing.
After pairing: runs silently in system tray.
Tray right-click: Pair / Repair Device | Unpair Device | Restart Agent | Quit
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
from tkinter import font as tkfont, filedialog
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

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[WARN] psutil not installed. Run: pip install psutil")

GPU_METHOD = None
try:
    import GPUtil
    GPU_METHOD = "gputil"
    print("[GPU] Using GPUtil (Nvidia)")
except ImportError:
    try:
        import wmi
        GPU_METHOD = "wmi"
        print("[GPU] Using WMI (AMD/Intel fallback)")
    except ImportError:
        print("[GPU] No GPU library available — GPU stats disabled")

PYCAW_AVAILABLE = False
if os.name == "nt":
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        PYCAW_AVAILABLE = True
        print("[AUDIO] pycaw available")
    except ImportError:
        print("[WARN] pycaw not installed. Run: pip install pycaw  (Windows volume mixer disabled)")

# ─────────────────────────────────────────────
# App data directory — always use %APPDATA%\PCControlHub
# so files persist correctly whether running as .py or .exe
# ─────────────────────────────────────────────
APP_NAME = "PCLink"

def get_app_dir() -> str:
    """Return the app data directory, creating it if needed."""
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~/.config")
    app_dir = os.path.join(base, APP_NAME)
    os.makedirs(app_dir, exist_ok=True)
    return app_dir

APP_DIR            = get_app_dir()
CONFIG_FILE        = os.path.join(APP_DIR, "config.json")
DEVICE_ID_FILE     = os.path.join(APP_DIR, "device_id.txt")
PAIRED_FILE        = os.path.join(APP_DIR, "paired.json")
STARTUP_QUEUE_FILE = os.path.join(APP_DIR, "startup_queue.json")

PAIR_CODE_TTL      = 120
STATS_INTERVAL     = 3
AUTOSTART_REG_NAME = "PCLinkAgent"
APP_VERSION        = "1.0.0"

# ─────────────────────────────────────────────
# Config — SERVER_URL stored in config.json,
# set during pairing from the QR code data
# ─────────────────────────────────────────────
def load_config() -> dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
    except: pass
    return {}

def save_config(data: dict):
    try:
        cfg = load_config()
        cfg.update(data)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[CONFIG] Save error: {e}")

_cfg       = load_config()
SERVER_URL = "wss://frothier-claire-enterologic.ngrok-free.dev"

# ─────────────────────────────────────────────
# Auto-start registry
# ─────────────────────────────────────────────
def get_exe_path() -> str:
    """Return the correct executable path whether running as .py or PyInstaller .exe."""
    if getattr(sys, "frozen", False):
        # Running as compiled PyInstaller exe
        return sys.executable
    else:
        # Running as Python script
        return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

def setup_autostart():
    if os.name != "nt":
        return
    try:
        import winreg
        exe_path = get_exe_path()
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, exe_path)
        print(f"[AUTOSTART] Registered: {exe_path}")
    except Exception as e:
        print(f"[AUTOSTART] Failed: {e}")

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

def get_device_mac() -> str:
    try:
        raw       = uuid.getnode()
        mac_bytes = raw.to_bytes(6, "big")
        return ":".join(f"{b:02X}" for b in mac_bytes)
    except Exception as e:
        print("[MAC ERROR]", e)
        return "00:00:00:00:00:00"

DEVICE_ID   = get_device_id()
DEVICE_NAME = get_device_name()
DEVICE_MAC  = get_device_mac()

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
# Startup queue
# Only executes when wake_triggered=True (i.e. Wake PC was step 1 of a scheduled event)
# This ensures files don't open unexpectedly on a normal PC boot
# ─────────────────────────────────────────────
def load_startup_queue() -> list:
    if not os.path.exists(STARTUP_QUEUE_FILE):
        return []
    try:
        with open(STARTUP_QUEUE_FILE, "r") as f:
            data = json.load(f)
        if data.get("wake_triggered", False):
            return data.get("steps", [])
        return []
    except:
        return []

def save_startup_queue(steps: list, wake_triggered: bool = False):
    with open(STARTUP_QUEUE_FILE, "w") as f:
        json.dump({"steps": steps, "wake_triggered": wake_triggered}, f)

def clear_startup_queue():
    if os.path.exists(STARTUP_QUEUE_FILE):
        try:
            os.remove(STARTUP_QUEUE_FILE)
        except:
            pass

def execute_startup_queue():
    """Run queued steps from a wake-first scheduled event. Called on first connect after boot."""
    steps = load_startup_queue()
    if not steps:
        return
    print(f"[STARTUP QUEUE] Executing {len(steps)} queued step(s)")
    clear_startup_queue()

    # Wait for desktop to be fully loaded and unlocked (max 3 minutes)
    print("[STARTUP QUEUE] Waiting for desktop to be ready...")
    for _ in range(180):
        time.sleep(1)
        if not is_session_locked():
            break
    else:
        print("[STARTUP QUEUE] Timed out waiting for unlock — skipping queue")
        return

    time.sleep(2)  # Extra buffer after unlock for desktop to settle
    print("[STARTUP QUEUE] Desktop ready, running steps")

    for step in steps:
        stype = step.get("type")
        if stype == "run_file":
            path = step.get("path", "")
            if path:
                print(f"[STARTUP QUEUE] Running: {path}")
                run_file(path)
                time.sleep(1)
        elif stype == "shutdown_pc":
            shutdown_pc()
        elif stype == "restart_pc":
            restart_pc()
        elif stype == "lock_pc":
            lock_pc()

# ─────────────────────────────────────────────
# Flags
# ─────────────────────────────────────────────
flags = {
    "show_qr":             False,
    "close_popup":         False,
    "show_unpaired":       False,
    "tray_unpair":         False,
    "tray_quit":           False,
    "tray_restart":        False,
    "we_initiated_unpair": False,
    "file_picker_request": None,
    "volume_subscribed":   False,
}

loop_ref      = {"loop": None}
ws_ref        = {"ws": None}
tray_ref      = {"icon": None}
popup_ref     = {"root": None}
pair_code_ref = {"code": ""}

# ─────────────────────────────────────────────
# PC stats
# ─────────────────────────────────────────────
def get_gpu_stats():
    if GPU_METHOD == "gputil":
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                g = gpus[0]
                return round(g.load * 100, 1), round(g.temperature, 1)
        except:
            pass
    elif GPU_METHOD == "wmi":
        try:
            w       = wmi.WMI(namespace="root\\OpenHardwareMonitor")
            sensors = w.Sensor()
            load = temp = None
            for s in sensors:
                if s.SensorType == "Load"        and "GPU" in s.Name: load = round(float(s.Value), 1)
                if s.SensorType == "Temperature" and "GPU" in s.Name: temp = round(float(s.Value), 1)
            return load, temp
        except:
            pass
    return None, None

def get_disk_stats() -> list:
    if not PSUTIL_AVAILABLE:
        return []
    EXCLUDED_FSTYPES = {"cdrom","udf","iso9660","squashfs","tmpfs","devtmpfs","devfs","overlay","proc","sysfs"}
    EXCLUDED_OPTS    = {"cdrom","remote"}
    disks = []
    try:
        for part in psutil.disk_partitions(all=False):
            if part.fstype.lower() in EXCLUDED_FSTYPES: continue
            if any(opt in part.opts.lower() for opt in EXCLUDED_OPTS): continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            label = part.device.rstrip("\\").rstrip("/") if os.name == "nt" else part.mountpoint
            disks.append({
                "label":    label,
                "used_gb":  round(usage.used  / (1024 ** 3), 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
                "percent":  round(usage.percent, 1),
            })
    except Exception as e:
        print("[DISK ERROR]", e)
    return disks

def collect_stats() -> dict:
    stats = {
        "device_id": DEVICE_ID, "cpu_percent": None, "cpu_temp": None,
        "ram_used_gb": None, "ram_total_gb": None, "ram_percent": None,
        "disks": [], "gpu_percent": None, "gpu_temp": None,
    }
    if not PSUTIL_AVAILABLE:
        return stats
    try:    stats["cpu_percent"] = psutil.cpu_percent(interval=None)
    except: pass
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for _, entries in temps.items():
                if entries: stats["cpu_temp"] = round(entries[0].current, 1); break
    except: pass
    try:
        ram = psutil.virtual_memory()
        stats["ram_used_gb"]  = round(ram.used  / (1024 ** 3), 1)
        stats["ram_total_gb"] = round(ram.total / (1024 ** 3), 1)
        stats["ram_percent"]  = ram.percent
    except: pass
    stats["disks"] = get_disk_stats()
    try:
        gpu_pct, gpu_temp    = get_gpu_stats()
        stats["gpu_percent"] = gpu_pct
        stats["gpu_temp"]    = gpu_temp
    except: pass
    return stats

# ─────────────────────────────────────────────
# PC commands
# /f flag forces processes to close even on a locked session (no admin needed)
# ─────────────────────────────────────────────
def _run_hidden(cmd: list) -> bool:
    """Run a Windows command without showing a console window."""
    import subprocess
    try:
        if os.name == "nt":
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[CMD ERROR] {e}")
        return False

def shutdown_pc() -> bool:
    print("[ACTION] Shutdown")
    return _run_hidden(["shutdown", "/s", "/t", "0", "/f"])

def restart_pc() -> bool:
    print("[ACTION] Restart")
    return _run_hidden(["shutdown", "/r", "/t", "0", "/f"])

def lock_pc() -> bool:
    print("[ACTION] Lock")
    return _run_hidden(["rundll32.exe", "user32.dll,LockWorkStation"])

# ─────────────────────────────────────────────
# Volume control (Windows only, requires pycaw)
# ─────────────────────────────────────────────
def is_session_locked() -> bool:
    """Check if the Windows session is currently locked."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
        if hdesk:
            ctypes.windll.user32.CloseDesktop(hdesk)
            return False
        return True
    except:
        return False

def get_volume_sessions() -> list:
    """Return all active audio sessions. Called via run_in_executor (already in a thread)."""
    if not PYCAW_AVAILABLE:
        return []
    sessions_out = []
    seen_names   = set()
    try:
        import pythoncom
        pythoncom.CoInitialize()
        sessions = AudioUtilities.GetAllSessions()
        for s in sessions:
            try:
                vol_iface = s._ctl.QueryInterface(ISimpleAudioVolume)
                volume    = round(vol_iface.GetMasterVolume() * 100)
                muted     = bool(vol_iface.GetMute())
                if s.Process:
                    proc_name = s.Process.name()
                    name = proc_name.replace(".exe","").replace(".EXE","")
                    friendly = {
                        "chrome":"Google Chrome","firefox":"Firefox","msedge":"Microsoft Edge",
                        "opera":"Opera","brave":"Brave","spotify":"Spotify","discord":"Discord",
                        "steam":"Steam","vlc":"VLC","mpc-hc64":"MPC-HC","mpc-hc":"MPC-HC",
                        "wmplayer":"Windows Media Player","groove":"Groove Music",
                        "zoom":"Zoom","teams":"Microsoft Teams","slack":"Slack",
                        "obs64":"OBS Studio","obs32":"OBS Studio",
                    }
                    display_name = friendly.get(name.lower(), name)
                    pid = str(s.ProcessId)
                else:
                    # Skip system sounds here — they're shown as master volume
                    continue
                key = display_name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                sessions_out.append({"id":pid,"name":display_name,"volume":volume,"muted":muted})
            except:
                pass
    except Exception as e:
        print(f"[VOLUME] Sessions error: {e}")
    finally:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except: pass
    return sessions_out

def get_master_volume() -> dict:
    """Return master volume level and mute state. Called via run_in_executor."""
    if not PYCAW_AVAILABLE:
        return {"volume": 50, "muted": False}
    try:
        import pythoncom
        pythoncom.CoInitialize()
        endpoint = _get_endpoint_volume()
        volume   = round(endpoint.GetMasterVolumeLevelScalar() * 100)
        muted    = bool(endpoint.GetMute())
        return {"volume": volume, "muted": muted}
    except Exception as e:
        print(f"[VOLUME] Master volume error: {e}")
        return {"volume": 50, "muted": False}
    finally:
        try:
            import pythoncom; pythoncom.CoUninitialize()
        except: pass

def _get_endpoint_volume():
    """Get IAudioEndpointVolume — bypasses pycaw version differences."""
    import comtypes.client
    # Use pycaw's own device enumeration but activate the interface ourselves
    from pycaw.pycaw import AudioUtilities
    speakers = AudioUtilities.GetSpeakers()
    # In newer pycaw, speakers is an AudioDevice with an _dev COM object inside
    if hasattr(speakers, '_dev'):
        device = speakers._dev
    elif hasattr(speakers, 'Activate'):
        device = speakers
    else:
        # Fall back: enumerate via IMMDeviceEnumerator through comtypes
        import comtypes
        IMMDeviceEnumerator_IID = comtypes.GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        MMDeviceEnumerator_CLSID = comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        enumerator = comtypes.CoCreateInstance(
            MMDeviceEnumerator_CLSID, None, CLSCTX_ALL, IMMDeviceEnumerator_IID
        )
        device = enumerator.GetDefaultAudioEndpoint(0, 1)

    iface    = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    endpoint = iface.QueryInterface(IAudioEndpointVolume)
    return endpoint

def set_master_volume(volume: float, muted: bool | None = None) -> bool:
    """Set the master volume level. Called via run_in_executor."""
    if not PYCAW_AVAILABLE:
        return False
    try:
        import pythoncom
        pythoncom.CoInitialize()
        endpoint = _get_endpoint_volume()
        endpoint.SetMasterVolumeLevelScalar(max(0.0, min(1.0, volume / 100)), None)
        if muted is not None:
            endpoint.SetMute(int(muted), None)
        print(f"[VOLUME] Master set to {volume}%")
        return True
    except Exception as e:
        print(f"[VOLUME] Set master error: {e}")
        return False
    finally:
        try:
            import pythoncom; pythoncom.CoUninitialize()
        except: pass

def set_session_volume(pid: str, volume: float, muted: bool | None = None) -> bool:
    """Set volume/mute for an audio session. Called via run_in_executor (already in a thread)."""
    if not PYCAW_AVAILABLE:
        return False
    try:
        import pythoncom
        pythoncom.CoInitialize()
        sessions = AudioUtilities.GetAllSessions()
        changed  = False
        for s in sessions:
            if str(s.ProcessId) == str(pid) or (pid == "0" and not s.Process):
                try:
                    vol_iface = s._ctl.QueryInterface(ISimpleAudioVolume)
                    vol_iface.SetMasterVolume(max(0.0, min(1.0, volume / 100)), None)
                    if muted is not None:
                        vol_iface.SetMute(int(muted), None)
                    changed = True
                except:
                    pass
        return changed
    except Exception as e:
        print(f"[VOLUME] Set session error: {e}")
        return False
    finally:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except: pass

def wake_on_lan(mac: str) -> bool:
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet    = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        print("[WOL] Sent")
        return True
    except Exception as e:
        print("[WOL ERROR]", e)
        return False

def find_steam_appid_for_path(exe_path: str):
    """
    Given a path inside steamapps/common/, try to find the Steam App ID
    by scanning the steamapps folder for .acf manifest files.
    Returns the appid string if found, else None.
    """
    try:
        import re
        path_lower = exe_path.replace("\\", "/").lower()
        # Extract game folder name from path (the folder directly under steamapps/common/)
        match = re.search(r"steamapps/common/([^/]+)", path_lower)
        if not match:
            return None
        game_folder = match.group(1)

        # Find steamapps directory
        steamapps_dir = re.search(r"(.+steamapps)/common/", exe_path.replace("\\", "/"), re.IGNORECASE)
        if not steamapps_dir:
            return None
        acf_dir = steamapps_dir.group(1)

        # Scan .acf manifest files for matching installdir
        import glob
        for acf_file in glob.glob(os.path.join(acf_dir, "appmanifest_*.acf")):
            try:
                with open(acf_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                installdir_match = re.search(r'"installdir"\s+"([^"]+)"', content)
                appid_match      = re.search(r'"appid"\s+"(\d+)"', content)
                if installdir_match and appid_match:
                    if installdir_match.group(1).lower() == game_folder:
                        return appid_match.group(1)
            except: pass
    except Exception as e:
        print(f"[STEAM DETECT] Error: {e}")
    return None

def find_epic_appid_for_path(exe_path: str):
    """
    Try to find the Epic Games launch ID for a given exe path by reading
    Epic's LauncherInstalled.dat file.
    Returns a launch URI string like com.epicgames.launcher://apps/APPNAME?action=launch
    """
    try:
        import re, json as _json
        dat_path = os.path.join(
            os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
            "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat"
        )
        if not os.path.exists(dat_path):
            return None
        with open(dat_path, "r", encoding="utf-8", errors="ignore") as f:
            data = _json.load(f)
        exe_norm = exe_path.replace("\\", "/").lower()
        for entry in data.get("InstallationList", []):
            install_dir = entry.get("InstallLocation", "").replace("\\", "/").lower()
            if install_dir and exe_norm.startswith(install_dir):
                app_name = entry.get("AppName", "")
                if app_name:
                    return f"com.epicgames.launcher://apps/{app_name}?action=launch&silent=true"
    except Exception as e:
        print(f"[EPIC DETECT] Error: {e}")
    return None

def run_file(path: str, run_as_admin: bool = False) -> bool:
    """
    Run an exe, script, steam:// URL, or any file with its default handler.
    If run_as_admin=True, uses ShellExecute with 'runas' verb on Windows to
    trigger a UAC elevation prompt and launch with admin privileges.

    Auto-detects Steam games (steamapps/common/...) and Epic Games installs,
    launching via their respective launcher URIs for proper dependency loading.
    Use a .lnk shortcut path for Xbox Game Pass or any other launcher.
    """
    print(f"[ACTION] Run: {path} (admin={run_as_admin})")
    try:
        path_lower = path.lower().replace("\\", "/")

        # Protocol URLs — pass through directly
        if any(path_lower.startswith(p) for p in ("steam://","com.epicgames","xbox://","http://","https://")):
            if os.name == "nt":
                os.startfile(path)
            else:
                import subprocess; subprocess.Popen(["xdg-open", path])
            return True

        # .lnk or .url shortcuts
        if path_lower.endswith(".lnk") or path_lower.endswith(".url"):
            if run_as_admin and os.name == "nt":
                import ctypes
                ctypes.windll.shell32.ShellExecuteW(None, "runas", path, None, None, 1)
            else:
                os.startfile(path)
            return True

        # Auto-detect Steam games
        if "steamapps" in path_lower and "common" in path_lower:
            appid = find_steam_appid_for_path(path)
            if appid:
                steam_url = f"steam://rungameid/{appid}"
                print(f"[ACTION] Steam game — launching via {steam_url}")
                os.startfile(steam_url) if os.name == "nt" else __import__("subprocess").Popen(["xdg-open", steam_url])
                return True
            print(f"[ACTION] Steam path — App ID not found, trying direct launch")

        # Auto-detect Epic Games
        if os.name == "nt":
            if any(d in path_lower for d in ["epic games","epicgames","fortnite"]):
                epic_uri = find_epic_appid_for_path(path)
                if epic_uri:
                    print(f"[ACTION] Epic game — launching via {epic_uri}")
                    os.startfile(epic_uri)
                    return True

        # Default launch
        if os.name == "nt":
            if run_as_admin:
                import ctypes
                # ShellExecute with 'runas' triggers UAC elevation prompt
                ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", path, None, None, 1)
                if ret <= 32:
                    print(f"[ACTION] ShellExecute runas failed: {ret}")
                    return False
            else:
                os.startfile(path)
        else:
            import subprocess; subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print(f"[RUN FILE ERROR] {e}")
        return False

# ─────────────────────────────────────────────
# WebSocket helpers
# ─────────────────────────────────────────────
async def _ws_send(payload: dict):
    ws = ws_ref.get("ws")
    if ws:
        try:    await ws.send(json.dumps(payload))
        except Exception as e: print("[WS SEND ERROR]", e)

def threadsafe_send(payload: dict):
    loop = loop_ref.get("loop")
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_ws_send(payload), loop)

async def _register_and_code(ws):
    await ws.send(json.dumps({
        "type": "register", "device_id": DEVICE_ID,
        "device_name": DEVICE_NAME, "device_mac": DEVICE_MAC, "is_paired": is_paired()
    }))
    if not is_paired():
        await ws.send(json.dumps({
            "type": "set_pair_code", "device_id": DEVICE_ID, "code": pair_code_ref["code"]
        }))

def threadsafe_register_and_code():
    ws = ws_ref.get("ws"); loop = loop_ref.get("loop")
    if ws and loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_register_and_code(ws), loop)

async def send_heartbeat(ws):
    while True:
        try:
            await ws.send(json.dumps({"type": "heartbeat", "device_id": DEVICE_ID, "timestamp": time.time()}))
            print("[HEARTBEAT] sent")
            await asyncio.sleep(10)
        except Exception as e:
            print("[HEARTBEAT ERROR]", e); break

async def send_stats_loop(ws):
    if PSUTIL_AVAILABLE: psutil.cpu_percent(interval=None)
    await asyncio.sleep(1)
    while True:
        try:
            stats = collect_stats()
            await ws.send(json.dumps({"type": "pc_stats", **stats}))
        except Exception as e:
            print("[STATS ERROR]", e); break
        await asyncio.sleep(STATS_INTERVAL)

async def send_volume_loop(ws):
    """Poll volume every 1s — but only while the mobile has the volume screen open."""
    if not PYCAW_AVAILABLE:
        return
    last_master   = None
    last_sessions = None
    while True:
        await asyncio.sleep(1)
        if not flags.get("volume_subscribed"):
            continue
        try:
            loop     = asyncio.get_event_loop()
            master   = await loop.run_in_executor(None, get_master_volume)
            sessions = await loop.run_in_executor(None, get_volume_sessions)
            if master != last_master or sessions != last_sessions:
                last_master   = master
                last_sessions = sessions
                await ws.send(json.dumps({
                    "type":      "volume_data",
                    "device_id": DEVICE_ID,
                    "master":    master,
                    "sessions":  sessions,
                }))
        except Exception as e:
            print("[VOLUME LOOP ERROR]", e); break

async def handle_command(cmd, ws):
    t      = cmd.get("type")
    cmd_id = cmd.get("command_id")
    if not t: return
    print(f"[COMMAND] {t}")

    status = "executed"
    try:
        if t == "shutdown_pc":
            if not shutdown_pc(): status = "failed"
        elif t == "restart_pc":
            if not restart_pc(): status = "failed"
        elif t == "lock_pc":
            if not lock_pc(): status = "failed"
        elif t == "wake_pc":
            mac = cmd.get("mac")
            if mac:
                if not wake_on_lan(mac): status = "failed"
            else:
                status = "failed"
        elif t == "run_custom_action":
            path         = cmd.get("path", "")
            run_as_admin = cmd.get("run_as_admin", False)
            if path:
                if not run_file(path, run_as_admin=run_as_admin): status = "failed"
            else:
                status = "failed"
        elif t == "open_file_picker":
            request_id = cmd.get("request_id", str(uuid.uuid4()))
            flags["file_picker_request"] = {"request_id": request_id}
            return
        elif t == "get_volume":
            flags["volume_subscribed"] = True
            loop     = asyncio.get_event_loop()
            master   = await loop.run_in_executor(None, get_master_volume)
            sessions = await loop.run_in_executor(None, get_volume_sessions)
            await ws.send(json.dumps({
                "type":      "volume_data",
                "device_id": DEVICE_ID,
                "master":    master,
                "sessions":  sessions,
            }))
            return
        elif t == "volume_subscribe":
            flags["volume_subscribed"] = True
            print("[VOLUME] Subscribed — polling active")
            return
        elif t == "volume_unsubscribe":
            flags["volume_subscribed"] = False
            print("[VOLUME] Unsubscribed — polling paused")
            return
        elif t == "set_master_volume":
            vol   = cmd.get("volume")
            muted = cmd.get("muted")
            if vol is not None:
                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(None, lambda: set_master_volume(float(vol), muted))
                if not ok: status = "failed"
            else:
                status = "failed"
        elif t == "set_session_volume":
            pid   = cmd.get("pid")
            vol   = cmd.get("volume")
            muted = cmd.get("muted")
            if pid is not None and vol is not None:
                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(None, lambda: set_session_volume(str(pid), float(vol), muted))
                if not ok: status = "failed"
            else:
                status = "failed"
        elif t == "save_startup_queue":
            # Server telling agent to save remaining steps for after-wake execution
            steps = cmd.get("steps", [])
            if steps:
                save_startup_queue(steps, wake_triggered=True)
                print(f"[STARTUP QUEUE] Saved {len(steps)} step(s) for post-wake execution")
            return
        elif t == "reload_agent":
            os._exit(0)
        elif t == "pair_confirmed":
            save_paired(True)
            print("[PAIRED] Saved to paired.json")
            flags["close_popup"] = True
        elif t == "unpaired":
            clear_paired()
            print("[UNPAIRED] Received from server")
            if flags["we_initiated_unpair"]:
                flags["we_initiated_unpair"] = False
            else:
                flags["show_unpaired"] = True
    except Exception as e:
        print("[COMMAND ERROR]", e)
        status = "failed"

    if cmd_id:
        await ws.send(json.dumps({"type": "ack", "command_id": cmd_id, "status": status}))

async def connect():
    # Check for startup queue before first connect (from wake-first scheduled events)
    startup_queue_pending  = bool(load_startup_queue())
    startup_queue_started  = False

    while True:
        try:
            # Use SSL for wss:// connections (ngrok), plain for ws://
            ssl_ctx = None
            if SERVER_URL.startswith("wss://"):
                import ssl
                ssl_ctx = ssl.create_default_context()
            async with websockets.connect(SERVER_URL, ssl=ssl_ctx,
                additional_headers={"ngrok-skip-browser-warning": "1"}) as ws:
                ws_ref["ws"] = ws
                print("[CONNECTED]")
                await ws.send(json.dumps({
                    "type": "register", "device_id": DEVICE_ID,
                    "device_name": DEVICE_NAME, "device_mac": DEVICE_MAC, "is_paired": is_paired()
                }))
                if not is_paired() and pair_code_ref["code"]:
                    await ws.send(json.dumps({
                        "type": "set_pair_code", "device_id": DEVICE_ID, "code": pair_code_ref["code"]
                    }))
                    print(f"[PAIR CODE REGISTERED] {pair_code_ref['code']}")

                # Execute startup queue on first connect only
                if startup_queue_pending and not startup_queue_started:
                    startup_queue_started = True
                    threading.Thread(target=execute_startup_queue, daemon=True).start()

                heartbeat_task = asyncio.create_task(send_heartbeat(ws))
                stats_task     = asyncio.create_task(send_stats_loop(ws))
                volume_task    = asyncio.create_task(send_volume_loop(ws))

                while True:
                    try:
                        msg  = await ws.recv()
                        data = json.loads(msg)
                        await handle_command(data, ws)
                    except websockets.ConnectionClosed:
                        print("[DISCONNECTED] reconnecting...")
                        heartbeat_task.cancel(); stats_task.cancel(); volume_task.cancel(); break
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
# Tray
# ─────────────────────────────────────────────
def tray_on_pair(icon, item):    flags["show_qr"]     = True
def tray_on_unpair(icon, item):  flags["tray_unpair"] = True
def tray_on_restart(icon, item): flags["tray_restart"]= True
def tray_on_quit(icon, item):    flags["tray_quit"]   = True

def make_tray_image():
    """Load the PCLink icon for the system tray, fall back to a blue dot."""
    # Check PyInstaller bundle temp dir first, then exe/script directory
    search_dirs = []
    if getattr(sys, "frozen", False):
        search_dirs.append(sys._MEIPASS)                          # bundled data files
        search_dirs.append(os.path.dirname(sys.executable))      # next to exe
    else:
        search_dirs.append(os.path.dirname(os.path.abspath(__file__)))  # next to script

    for base_dir in search_dirs:
        for name in ("icon.ico", "icon.png", "pclink-icon.png"):
            path = os.path.join(base_dir, name)
            if os.path.exists(path):
                try:
                    img = Image.open(path).convert("RGBA").resize((64, 64), Image.LANCZOS)
                    return img
                except: pass

    # Fallback — blue dot matching app accent color
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill="#007aff")
    return img

def start_tray():
    if not TRAY_AVAILABLE: return
    menu = pystray.Menu(
        pystray.MenuItem("Pair / Repair Device", tray_on_pair),
        pystray.MenuItem("Unpair Device",         tray_on_unpair),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart Agent",         tray_on_restart),
        pystray.MenuItem("Quit",                  tray_on_quit),
    )
    icon = pystray.Icon("PCLink", make_tray_image(), "PCLink", menu)
    tray_ref["icon"] = icon
    icon.run()

# ─────────────────────────────────────────────
# File picker — must run on main thread (tkinter requirement)
# ─────────────────────────────────────────────
def handle_file_picker_request(request_id: str):
    """Opens a Windows file dialog and sends the selected path back via WebSocket."""
    try:
        root = tk.Tk()
        root.withdraw()
        # Prevent maximize — maximized tkinter windows can't be dragged by title bar
        root.resizable(True, True)
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.update()
        # Remove topmost after 200ms so user can switch windows behind it
        root.after(200, lambda: root.attributes("-topmost", False))
        path = filedialog.askopenfilename(
            title="Select File for Custom Action — PCLink",
            filetypes=[
                ("Executables & Shortcuts", "*.exe *.bat *.cmd *.ps1 *.lnk"),
                ("All Files", "*.*"),
            ],
            parent=root,
        )
        root.destroy()
        result = path if path else None
        print(f"[FILE PICKER] Selected: {result}")
    except Exception as e:
        print(f"[FILE PICKER ERROR] {e}")
        result = None

    threadsafe_send({
        "type":       "file_picker_result",
        "request_id": request_id,
        "path":       result,
        "device_id":  DEVICE_ID,
    })

# ─────────────────────────────────────────────
# Custom topmost dialog
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# Design tokens — matches mobile app aesthetic
# ─────────────────────────────────────────────
UI = {
    "bg":           "#0b0f14",   # deep dark background
    "surface":      "#1c2130",   # card/surface background
    "surface2":     "#263044",   # slightly lighter surface
    "border":       "#2a3248",   # subtle border
    "accent":       "#007aff",   # iOS blue
    "accent_green": "#22c55e",   # online / success green
    "accent_red":   "#ef4444",   # destructive / error
    "accent_amber": "#f59e0b",   # warning
    "text":         "#ffffff",   # primary text
    "text_sub":     "#8e9bb5",   # secondary text
    "text_muted":   "#4a5568",   # muted text / code
    "code_bg":      "#141923",   # code block background
    "code_fg":      "#64d9ff",   # code text (cyan)
    "btn_primary":  "#007aff",
    "btn_danger":   "#ef4444",
    "btn_neutral":  "#263044",
    "radius":       8,
}

def _center(win, w, h):
    sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

def _font(size=10, weight="normal", family="Segoe UI"):
    return tkfont.Font(family=family, size=size, weight=weight)

def _styled_button(parent, text, bg, fg="#ffffff", command=None, width=None):
    """A flat, rounded-look button matching the mobile aesthetic."""
    kw = dict(
        text=text, font=_font(10, "bold"), bg=bg, fg=fg,
        relief="flat", bd=0, padx=20, pady=8,
        activebackground=bg, activeforeground=fg,
        cursor="hand2",
    )
    if width: kw["width"] = width
    if command: kw["command"] = command
    btn = tk.Button(parent, **kw)
    return btn

def _topmost_dialog(title: str, message: str, kind: str = "yesno", extra_button: str | None = None):
    result = [None]
    dlg = tk.Tk()
    dlg.title("PCLink")
    dlg.configure(bg=UI["bg"])
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)
    dlg.lift(); dlg.focus_force()

    is_info   = kind == "info"
    is_triple = kind == "triple"
    h = 160 if is_info else 190 if not is_triple else 210
    _center(dlg, 420, h)

    # Title bar accent line
    tk.Frame(dlg, bg=UI["accent"], height=3).pack(fill="x")

    # Title label
    tk.Label(dlg, text=title, font=_font(12, "bold"),
             bg=UI["bg"], fg=UI["text"], anchor="w",
             padx=24, pady=12).pack(fill="x")

    # Divider
    tk.Frame(dlg, bg=UI["border"], height=1).pack(fill="x", padx=0)

    # Message
    tk.Label(dlg, text=message, font=_font(10),
             bg=UI["bg"], fg=UI["text_sub"],
             wraplength=372, justify="left",
             padx=24, pady=16, anchor="w").pack(fill="x")

    # Buttons
    btn_frame = tk.Frame(dlg, bg=UI["bg"], padx=20, pady=0)
    btn_frame.pack(fill="x")

    if is_info:
        _styled_button(btn_frame, "OK", UI["btn_primary"],
                       command=dlg.destroy).pack(side="right", padx=(6,0))
    elif kind == "yesno":
        def on_yes(): result[0]=True;  dlg.destroy()
        def on_no():  result[0]=False; dlg.destroy()
        _styled_button(btn_frame, "Yes", UI["btn_primary"], command=on_yes).pack(side="right", padx=(6,0))
        _styled_button(btn_frame, "No",  UI["btn_neutral"], command=on_no).pack(side="right",  padx=(6,0))
    elif is_triple:
        def on_yes():   result[0]="yes";   dlg.destroy()
        def on_extra(): result[0]="extra"; dlg.destroy()
        def on_no():    result[0]="no";    dlg.destroy()
        _styled_button(btn_frame, "Yes",                   UI["btn_primary"],  command=on_yes).pack(side="right",   padx=(6,0))
        _styled_button(btn_frame, extra_button or "Other", UI["accent_green"], command=on_extra).pack(side="right", padx=(6,0))
        _styled_button(btn_frame, "No",                    UI["btn_neutral"],  command=on_no).pack(side="right",    padx=(6,0))

    dlg.mainloop()
    return result[0]

# ─────────────────────────────────────────────
# Main-thread UI
# ─────────────────────────────────────────────
def close_popup_if_open():
    root = popup_ref.get("root")
    if root:
        try: root.destroy()
        except: pass
        popup_ref["root"] = None

def _fresh_code() -> str:
    code = str(random.randint(100000, 999999))
    pair_code_ref["code"] = code
    return code

def _do_unpair_and_notify():
    flags["we_initiated_unpair"] = True
    clear_paired()
    threadsafe_send({"type": "unpair_from_pc", "device_id": DEVICE_ID})
    threadsafe_send({"type": "register", "device_id": DEVICE_ID, "device_name": DEVICE_NAME, "device_mac": DEVICE_MAC, "is_paired": False})
    print("[AGENT] Unpaired and notified server")

def show_pair_popup():
    close_popup_if_open()
    code = _fresh_code()
    print(f"[NEW PAIR CODE] {code}")
    threadsafe_register_and_code()
    time.sleep(0.4)

    root = tk.Tk()
    popup_ref["root"] = root
    root.title("PCLink — Pair Device")
    root.configure(bg=UI["bg"])
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.lift(); root.focus_force()

    w = 360; h = 530 if QR_AVAILABLE else 320
    _center(root, w, h)

    # Top accent bar
    tk.Frame(root, bg=UI["accent"], height=3).pack(fill="x")

    # Header
    hdr = tk.Frame(root, bg=UI["bg"])
    hdr.pack(fill="x", padx=24, pady=(18, 0))
    tk.Label(hdr, text="PCLink", font=_font(15, "bold"),
             bg=UI["bg"], fg=UI["text"]).pack(anchor="w")
    tk.Label(hdr, text="Pair a new device", font=_font(10),
             bg=UI["bg"], fg=UI["text_sub"]).pack(anchor="w", pady=(2,0))

    # Divider
    tk.Frame(root, bg=UI["border"], height=1).pack(fill="x", padx=24, pady=(12,0))

    if QR_AVAILABLE:
        # Instructions
        tk.Label(root, text="Scan QR code with the PCLink app",
                 font=_font(9), bg=UI["bg"], fg=UI["text_sub"]).pack(pady=(10,6))

        # QR code — white card
        qr_data = json.dumps({"server": SERVER_URL, "code": code})
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=3)
        qr.add_data(qr_data); qr.make(fit=True)
        qr_img  = qr.make_image(fill_color="#0b0f14", back_color="white")
        buf     = BytesIO(); qr_img.save(buf, format="PNG"); buf.seek(0)
        pil_img = Image.open(buf).resize((210, 210), Image.NEAREST)
        tk_img  = ImageTk.PhotoImage(pil_img)
        qr_card = tk.Frame(root, bg="white", padx=6, pady=6, relief="flat")
        qr_card.pack(pady=(0,12))
        tk.Label(qr_card, image=tk_img, bg="white").pack()
        root._qr_img = tk_img

    # Divider with "or"
    or_row = tk.Frame(root, bg=UI["bg"]); or_row.pack(fill="x", padx=24, pady=(0,8))
    tk.Frame(or_row, bg=UI["border"], height=1).pack(side="left", fill="x", expand=True, pady=6)
    tk.Label(or_row, text="  manual code  ", font=_font(8), bg=UI["bg"], fg=UI["text_muted"]).pack(side="left")
    tk.Frame(or_row, bg=UI["border"], height=1).pack(side="left", fill="x", expand=True, pady=6)

    # Code display
    code_card = tk.Frame(root, bg=UI["surface"], padx=20, pady=12)
    code_card.pack(padx=24, fill="x")
    tk.Label(code_card, text=f"{code[:3]}  {code[3:]}", font=_font(28, "bold", "Courier New"),
             bg=UI["surface"], fg=UI["code_fg"]).pack()

    # Expiry + server info
    timer_var = tk.StringVar(value=f"Expires in {PAIR_CODE_TTL}s")
    tk.Label(root, textvariable=timer_var, font=_font(8),
             bg=UI["bg"], fg=UI["text_muted"]).pack(pady=(6,0))
    tk.Label(root, text=f"Server: {SERVER_URL}", font=_font(8),
             bg=UI["bg"], fg=UI["text_muted"]).pack(pady=(2,10))

    remaining = [PAIR_CODE_TTL]
    def tick():
        if is_paired(): root.destroy(); return
        remaining[0] -= 1
        if remaining[0] > 0:
            timer_var.set(f"Expires in {remaining[0]}s"); root.after(1000, tick)
        else:
            if not is_paired(): root.destroy(); flags["show_qr"] = True
            else: root.destroy()
    root.after(1000, tick)
    root.mainloop()
    popup_ref["root"] = None

def handle_show_qr():
    if is_paired():
        answer = _topmost_dialog(
            "Already Paired",
            f"{DEVICE_NAME} is already paired to a phone.\n\nWould you like to unpair and pair with a new phone instead?",
            kind="triple", extra_button="Unpair & Repair"
        )
        if answer == "extra":
            _do_unpair_and_notify(); time.sleep(0.3); show_pair_popup()
        return
    show_pair_popup()

def handle_unpaired_dialog():
    repair = _topmost_dialog(
        "Device Unpaired",
        "Your phone has been disconnected from this PC.\n\nWould you like to pair with a new phone?",
        kind="yesno"
    )
    if repair:
        show_pair_popup()
    else:
        uninstall = _topmost_dialog(
            "Uninstall PCLink?",
            "Would you like to uninstall PCLink from this PC?",
            kind="yesno"
        )
        if uninstall:
            _topmost_dialog("PCLink",
                "The application will now close.\nYou can uninstall it from Windows Settings → Apps.",
                kind="info")
            sys.exit(0)
        else:
            print("[AGENT] Running unpaired in background.")

def handle_tray_unpair():
    if not is_paired():
        _topmost_dialog("Not Paired", "This PC is not currently paired to any phone.", kind="info"); return
    result = _topmost_dialog("Unpair Device",
        f"This will disconnect your phone from {DEVICE_NAME}.\n\nAre you sure you want to unpair?",
        kind="yesno")
    if not result: return
    _do_unpair_and_notify()
    again = _topmost_dialog("Pair New Device?",
        "Would you like to pair this PC with a new phone?", kind="yesno")
    if again: show_pair_popup()

def handle_tray_restart():
    """Restart the agent process in place."""
    print("[RESTART] Restarting agent...")
    try:
        icon = tray_ref.get("icon")
        if icon: icon.stop()
    except: pass
    time.sleep(0.3)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[AGENT] v{APP_VERSION}")
    print(f"[AGENT] Device:    {DEVICE_NAME} ({DEVICE_ID})")
    print(f"[AGENT] MAC:       {DEVICE_MAC}")
    print(f"[AGENT] Data dir:  {APP_DIR}")
    print(f"[AGENT] Server:    {SERVER_URL}")
    print(f"[AGENT] Paired:    {is_paired()}")
    print(f"[AGENT] psutil:    {PSUTIL_AVAILABLE}")
    print(f"[AGENT] GPU:       {GPU_METHOD or 'unavailable'}")

    setup_autostart()

    def handle_sigint(sig, frame):
        print("\n[EXIT] Agent stopped."); sys.exit(0)
    signal.signal(signal.SIGINT, handle_sigint)

    bg_thread = threading.Thread(target=run_async, daemon=True)
    bg_thread.start()

    if TRAY_AVAILABLE:
        tray_thread = threading.Thread(target=start_tray, daemon=True)
        tray_thread.start()

    time.sleep(0.8)

    if not is_paired():
        _fresh_code()
        show_pair_popup()

    print("[AGENT] Running. Right-click the tray icon for options.")
    while True:
        time.sleep(0.4)
        if flags["tray_quit"]:
            flags["tray_quit"] = False
            print("[EXIT] Quit from tray."); sys.exit(0)
        if flags["tray_restart"]:
            flags["tray_restart"] = False
            handle_tray_restart()
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
        fp = flags.get("file_picker_request")
        if fp:
            flags["file_picker_request"] = None
            handle_file_picker_request(fp["request_id"])