import os
import sys
import time
import json
import shutil
import ctypes
import subprocess
import webbrowser
import threading
import winreg
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu

# Ensure UTF-8 stdout on Windows
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

APP_NAME = "MediaRemoteDownloader"

_MUTEX_HANDLE = None

def acquire_single_instance_lock():
    global _MUTEX_HANDLE
    if os.name != 'nt':
        return True
    try:
        mutex_name = "Local\\MediaRemoteDownloader_SingleInstance_Mutex"
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        last_error = ctypes.windll.kernel32.GetLastError()
        if not _MUTEX_HANDLE or last_error == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True

# Determine project directory cleanly for both source execution and PyInstaller frozen binary
if getattr(sys, 'frozen', False):
    PROJECT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DOWNLOAD_DIR = "U:\\movies"
REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_FILE = os.path.join(PROJECT_DIR, "config", "tray_config.json")

# Default Configuration
DEFAULT_CONFIG = {
    "mode": "docker",  # "docker" or "native"
    "auto_launch_docker": True,
    "auto_start_containers": True
}

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(data)
                return config
    except Exception as e:
        print(f"Error loading config: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

# Global native bot process reference
NATIVE_BOT_PROCESS = None

def is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False

def set_autostart(enable=True):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        if enable:
            if getattr(sys, 'frozen', False):
                exe_path = f'"{os.path.abspath(sys.executable)}"'
            else:
                exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Error updating autostart registry: {e}")

def find_docker_desktop_exe():
    possible_paths = [
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        r"C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Docker\Docker\Docker Desktop.exe")
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    exe_in_path = shutil.which("Docker Desktop.exe")
    if exe_in_path:
        return exe_in_path
        
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Docker Inc.\Docker Desktop", 0, winreg.KEY_READ)
        install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        exe_path = os.path.join(install_path, "Docker Desktop.exe")
        if os.path.exists(exe_path):
            return exe_path
    except Exception:
        pass
    return None

def is_docker_cli_available():
    try:
        res = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        return res.returncode == 0
    except Exception:
        return False

def is_docker_engine_running():
    try:
        res = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        return res.returncode == 0
    except Exception:
        return False

def check_services_running():
    if not is_docker_engine_running():
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        if result.returncode == 0 and ("media_downloader_bot" in result.stdout or "running" in result.stdout.lower()):
            return True
        return False
    except Exception:
        return False

def launch_docker_desktop():
    exe = find_docker_desktop_exe()
    if exe:
        try:
            subprocess.Popen([exe], creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return True
        except Exception as e:
            print(f"Error launching Docker Desktop: {e}")
    return False

def get_python_interpreter():
    venv_py = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        return venv_py
    py_path = shutil.which("python") or shutil.which("py")
    if py_path:
        return py_path
    if not getattr(sys, 'frozen', False):
        return sys.executable
    return "python"

def is_native_bot_running():
    global NATIVE_BOT_PROCESS
    if NATIVE_BOT_PROCESS and NATIVE_BOT_PROCESS.poll() is None:
        return True
    return False

def start_native_bot():
    global NATIVE_BOT_PROCESS
    if is_native_bot_running():
        return True
    bot_script = os.path.join(PROJECT_DIR, "bot.py")
    if not os.path.exists(bot_script):
        print(f"bot.py not found at {bot_script}")
        return False
    py_exe = get_python_interpreter()
    try:
        NATIVE_BOT_PROCESS = subprocess.Popen(
            [py_exe, bot_script],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        return True
    except Exception as e:
        print(f"Failed to start native bot: {e}")
        return False

def stop_native_bot():
    global NATIVE_BOT_PROCESS
    if NATIVE_BOT_PROCESS and NATIVE_BOT_PROCESS.poll() is None:
        try:
            NATIVE_BOT_PROCESS.terminate()
            NATIVE_BOT_PROCESS.wait(timeout=3)
        except Exception:
            try:
                NATIVE_BOT_PROCESS.kill()
            except Exception:
                pass
    NATIVE_BOT_PROCESS = None

def create_icon_image(status="active"):
    # status: "active" (green), "starting" (yellow), "inactive" (red), "nodocker" (gray)
    img = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    
    # Outer dark circle
    d.ellipse((4, 4, 60, 60), fill=(24, 28, 36, 255))
    
    # Movie Play Triangle Symbol
    d.polygon([(26, 20), (26, 44), (46, 32)], fill=(255, 255, 255, 255))
    
    # Status Indicator Light in Bottom-Right
    if status == "active":
        color = (46, 204, 113, 255)  # Green
    elif status == "starting":
        color = (241, 196, 15, 255)  # Yellow
    elif status == "nodocker":
        color = (149, 165, 166, 255) # Gray
    else:
        color = (231, 76, 60, 255)   # Red

    d.ellipse((42, 42, 58, 58), fill=color, outline=(24, 28, 36, 255), width=2)
    return img

class LogViewerWindow:
    def __init__(self, mode_getter):
        self.root = None
        self.running = False
        self.mode_getter = mode_getter

    def show(self):
        if self.root and self.root.winfo_exists():
            self.root.deiconify()
            self.root.focus_force()
            return

        self.root = tk.Tk()
        mode_str = self.mode_getter().title()
        self.root.title(f"Media Remote Downloader - Activity Log ({mode_str} Mode)")
        self.root.geometry("820x520")
        self.root.configure(bg="#1e1e2e")

        title_label = tk.Label(
            self.root,
            text=f"🎬 Live Bot Activity Log ({mode_str} Mode)",
            font=("Segoe UI", 12, "bold"),
            fg="#cdd6f4",
            bg="#1e1e2e",
            pady=8
        )
        title_label.pack(fill=tk.X)

        self.log_text = scrolledtext.ScrolledText(
            self.root,
            wrap=tk.WORD,
            bg="#11111b",
            fg="#a6adc8",
            insertbackground="white",
            font=("Consolas", 10)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.running = True
        threading.Thread(target=self.stream_logs, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()

    def stream_logs(self):
        mode = self.mode_getter()
        if mode == "docker":
            try:
                process = subprocess.Popen(
                    ["docker", "logs", "--tail", "100", "-f", "media_downloader_bot"],
                    cwd=PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                for line in iter(process.stdout.readline, ''):
                    if not self.running:
                        process.terminate()
                        break
                    if self.root and self.root.winfo_exists():
                        self.log_text.insert(tk.END, line)
                        self.log_text.see(tk.END)
            except Exception as e:
                if self.root and self.root.winfo_exists():
                    self.log_text.insert(tk.END, f"\n[Error streaming Docker logs: {e}]\n")
        else: # Native Mode
            global NATIVE_BOT_PROCESS
            if NATIVE_BOT_PROCESS and NATIVE_BOT_PROCESS.stdout:
                try:
                    for line in iter(NATIVE_BOT_PROCESS.stdout.readline, ''):
                        if not self.running:
                            break
                        if self.root and self.root.winfo_exists():
                            self.log_text.insert(tk.END, line)
                            self.log_text.see(tk.END)
                except Exception as e:
                    if self.root and self.root.winfo_exists():
                        self.log_text.insert(tk.END, f"\n[Error streaming Native logs: {e}]\n")
            else:
                if self.root and self.root.winfo_exists():
                    self.log_text.insert(tk.END, "\n[Native bot process is not running or output stream unavailable]\n")

    def on_close(self):
        self.running = False
        if self.root:
            self.root.destroy()
            self.root = None

class TrayApp:
    def __init__(self):
        self.config = load_config()
        self.log_viewer = LogViewerWindow(mode_getter=lambda: self.config.get("mode", "docker"))
        self.running = True
        self.icon = None
        self.docker_starting_flag = False

    def on_open_folder(self, icon, item):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        os.startfile(DOWNLOAD_DIR)

    def on_open_jackett(self, icon, item):
        webbrowser.open("http://localhost:9117")

    def on_open_qbittorrent(self, icon, item):
        webbrowser.open("http://localhost:8080")

    def on_show_logs(self, icon, item):
        threading.Thread(target=self.log_viewer.show, daemon=True).start()

    def on_toggle_autostart(self, icon, item):
        currently_enabled = is_autostart_enabled()
        set_autostart(not currently_enabled)

    def on_toggle_auto_launch_docker(self, icon, item):
        self.config["auto_launch_docker"] = not self.config.get("auto_launch_docker", True)
        save_config(self.config)

    def on_toggle_auto_start_containers(self, icon, item):
        self.config["auto_start_containers"] = not self.config.get("auto_start_containers", True)
        save_config(self.config)

    def on_switch_mode(self, icon, item):
        current_mode = self.config.get("mode", "docker")
        new_mode = "native" if current_mode == "docker" else "docker"
        self.config["mode"] = new_mode
        save_config(self.config)
        
        # Handle switching active services
        if new_mode == "native":
            # Stop docker containers if running
            def switch_to_native():
                if check_services_running():
                    subprocess.run(["docker", "compose", "stop"], cwd=PROJECT_DIR, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                start_native_bot()
                self.update_icon_and_menu()
            threading.Thread(target=switch_to_native, daemon=True).start()
        else:
            # Switch to docker
            def switch_to_docker():
                stop_native_bot()
                if is_docker_engine_running():
                    subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_DIR, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                else:
                    if find_docker_desktop_exe():
                        launch_docker_desktop()
                self.update_icon_and_menu()
            threading.Thread(target=switch_to_docker, daemon=True).start()

    def on_launch_docker_desktop(self, icon, item):
        def launch():
            self.docker_starting_flag = True
            self.update_icon_and_menu()
            if launch_docker_desktop():
                if self.icon:
                    self.icon.notify("Docker Desktop launched. Waiting for Docker engine to become ready...", APP_NAME)
            else:
                if self.icon:
                    self.icon.notify("Failed to launch Docker Desktop. Path not found.", APP_NAME)
            self.docker_starting_flag = False
            self.update_icon_and_menu()
        threading.Thread(target=launch, daemon=True).start()

    def on_restart_services(self, icon, item):
        def restart():
            if self.config.get("mode") == "docker":
                subprocess.run(
                    ["docker", "compose", "up", "-d", "--force-recreate"],
                    cwd=PROJECT_DIR,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            else:
                stop_native_bot()
                start_native_bot()
            self.update_icon_and_menu()
        threading.Thread(target=restart, daemon=True).start()

    def on_toggle_services(self, icon, item):
        def toggle():
            if self.config.get("mode") == "docker":
                if check_services_running():
                    subprocess.run(
                        ["docker", "compose", "stop"],
                        cwd=PROJECT_DIR,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    )
                else:
                    if not is_docker_engine_running():
                        if find_docker_desktop_exe():
                            launch_docker_desktop()
                    subprocess.run(
                        ["docker", "compose", "up", "-d"],
                        cwd=PROJECT_DIR,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    )
            else:
                if is_native_bot_running():
                    stop_native_bot()
                else:
                    start_native_bot()
            self.update_icon_and_menu()
        threading.Thread(target=toggle, daemon=True).start()

    def on_exit(self, icon, item):
        self.running = False
        if self.config.get("mode") == "native":
            stop_native_bot()
        self.icon.stop()

    def get_status_info(self):
        mode = self.config.get("mode", "docker")
        if mode == "docker":
            engine_running = is_docker_engine_running()
            if not engine_running:
                if self.docker_starting_flag:
                    return "starting", "🟡 Docker Engine Starting...", False
                docker_exe = find_docker_desktop_exe()
                if not docker_exe:
                    return "nodocker", "⚠️ Docker Desktop Not Installed", False
                return "inactive", "🔴 Docker Engine Stopped", False
            
            services_up = check_services_running()
            if services_up:
                return "active", "🟢 Bot Active (Docker)", True
            else:
                return "inactive", "🔴 Bot Stopped (Docker)", False
        else: # Native Mode
            native_up = is_native_bot_running()
            if native_up:
                return "active", "🟢 Bot Active (Native Python)", True
            else:
                return "inactive", "🔴 Bot Stopped (Native Python)", False

    def update_icon_and_menu(self):
        if not self.icon:
            return
        status_key, status_text, is_running = self.get_status_info()
        self.icon.icon = create_icon_image(status_key)
        self.icon.title = f"{APP_NAME} ({status_text})"
        self.icon.menu = self.build_menu()

    def build_menu(self):
        status_key, status_text, is_running = self.get_status_info()
        mode = self.config.get("mode", "docker")
        docker_installed = find_docker_desktop_exe() is not None

        menu_items = [
            item(status_text, lambda icon, item: None, enabled=False),
            item(f"⚙️ Mode: {'🐳 Docker' if mode == 'docker' else '🐍 Native Python'}", lambda icon, item: None, enabled=False),
            Menu.SEPARATOR
        ]

        if mode == "docker" and not is_docker_engine_running() and docker_installed:
            menu_items.append(item("🚀 Launch Docker Desktop", self.on_launch_docker_desktop))
            menu_items.append(Menu.SEPARATOR)

        start_stop_text = "🛑 Stop Bot Services" if is_running else ("▶️ Start Bot Services" if mode == "docker" else "▶️ Start Native Bot")
        menu_items.extend([
            item(start_stop_text, self.on_toggle_services),
            item("🔄 Restart Bot Services", self.on_restart_services),
            item(f"🔀 Switch to {'Native Python' if mode == 'docker' else 'Docker'} Mode", self.on_switch_mode),
            item("📜 View Live Activity Logs", self.on_show_logs),
            Menu.SEPARATOR,
            item("📁 Open Downloads Folder (U:\\movies)", self.on_open_folder),
            item("🌐 Open Jackett Indexer UI", self.on_open_jackett),
            item("⏬ Open qBittorrent UI", self.on_open_qbittorrent),
            Menu.SEPARATOR,
            item("⚙️ Options", Menu(
                item("Windows Autostart", self.on_toggle_autostart, checked=lambda item: is_autostart_enabled()),
                item("Auto-Launch Docker Desktop on Boot", self.on_toggle_auto_launch_docker, checked=lambda item: self.config.get("auto_launch_docker", True)),
                item("Auto-Start Containers when Docker Ready", self.on_toggle_auto_start_containers, checked=lambda item: self.config.get("auto_start_containers", True))
            )),
            Menu.SEPARATOR,
            item("❌ Exit", self.on_exit)
        ])

        return Menu(*menu_items)

    def run_status_loop(self):
        last_engine_state = False
        initial_check = True

        while self.running:
            try:
                mode = self.config.get("mode", "docker")

                if mode == "docker":
                    engine_running = is_docker_engine_running()
                    docker_exe = find_docker_desktop_exe()

                    # Auto-launch Docker Desktop on startup if enabled and engine is stopped
                    if initial_check:
                        if not engine_running and self.config.get("auto_launch_docker", True) and docker_exe:
                            self.docker_starting_flag = True
                            launch_docker_desktop()
                            if self.icon:
                                self.icon.notify("Launching Docker Desktop in background...", APP_NAME)
                        elif not engine_running and not docker_exe:
                            if self.icon:
                                self.icon.notify("Docker Desktop is not installed. You can switch to Native Python mode from tray menu.", APP_NAME)

                    # Auto-start containers when Docker engine transitions from down -> up
                    if engine_running and (not last_engine_state or initial_check):
                        self.docker_starting_flag = False
                        if self.config.get("auto_start_containers", True) and not check_services_running():
                            subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_DIR, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

                    last_engine_state = engine_running
                else: # Native Mode
                    if initial_check and not is_native_bot_running():
                        start_native_bot()

                initial_check = False
                self.update_icon_and_menu()
            except Exception as e:
                print(f"Error in status loop: {e}")

            time.sleep(5)

    def run(self):
        status_key, status_text, _ = self.get_status_info()

        self.icon = pystray.Icon(
            APP_NAME,
            create_icon_image(status_key),
            f"Media Remote Downloader ({status_text})",
            menu=self.build_menu()
        )

        threading.Thread(target=self.run_status_loop, daemon=True).start()
        self.icon.run()

if __name__ == '__main__':
    if not acquire_single_instance_lock():
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, "Media Remote Downloader is already running in the system tray.")
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    app = TrayApp()
    app.run()
