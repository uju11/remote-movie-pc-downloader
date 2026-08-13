import os
import sys
import time
import subprocess
import webbrowser
import threading
import winreg
import tkinter as tk
from tkinter import ttk, scrolledtext
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu

APP_NAME = "MediaRemoteDownloader"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = "U:\\movies"
REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

def is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False

def set_autostart(enable=True):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        if enable:
            exe_path = sys.argv[0]
            if not exe_path.endswith('.exe'):
                exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            else:
                exe_path = f'"{os.path.abspath(exe_path)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Error updating autostart registry: {e}")

def check_services_running():
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        if result.returncode == 0 and ("media_downloader_bot" in result.stdout or "running" in result.stdout.lower()):
            return True
        return False
    except Exception:
        return False

def create_icon_image(running=True):
    # Draw a 64x64 icon
    img = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    
    # Outer dark rounded rectangle / circle
    d.ellipse((4, 4, 60, 60), fill=(24, 28, 36, 255))
    
    # Movie Play Triangle / Reel Symbol
    d.polygon([(26, 20), (26, 44), (46, 32)], fill=(255, 255, 255, 255))
    
    # Status Indicator Light in Bottom-Right
    status_color = (46, 204, 113, 255) if running else (231, 76, 60, 255)
    d.ellipse((42, 42, 58, 58), fill=status_color, outline=(24, 28, 36, 255), width=2)
    
    return img

class LogViewerWindow:
    def __init__(self):
        self.root = None
        self.running = False

    def show(self):
        if self.root and self.root.winfo_exists():
            self.root.deiconify()
            self.root.focus_force()
            return

        self.root = tk.Tk()
        self.root.title("Media Remote Downloader - Live Activity Log")
        self.root.geometry("800x500")
        self.root.configure(bg="#1e1e2e")

        # Set window icon if possible
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        title_label = tk.Label(
            self.root,
            text="🎬 Live Bot & Downloader Activity",
            font=("Segoe UI", 13, "bold"),
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
        try:
            process = subprocess.Popen(
                ["docker", "logs", "--tail", "50", "-f", "media_downloader_bot"],
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
                self.log_text.insert(tk.END, f"\n[Error streaming logs: {e}]\n")

    def on_close(self):
        self.running = False
        if self.root:
            self.root.destroy()
            self.root = None

class TrayApp:
    def __init__(self):
        self.log_viewer = LogViewerWindow()
        self.running = True
        self.icon = None

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

    def on_restart_services(self, icon, item):
        def restart():
            subprocess.run(
                ["docker", "compose", "restart"],
                cwd=PROJECT_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            self.update_icon()
        threading.Thread(target=restart, daemon=True).start()

    def on_toggle_services(self, icon, item):
        def toggle():
            if check_services_running():
                subprocess.run(
                    ["docker", "compose", "stop"],
                    cwd=PROJECT_DIR,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            else:
                subprocess.run(
                    ["docker", "compose", "up", "-d"],
                    cwd=PROJECT_DIR,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            self.update_icon()
        threading.Thread(target=toggle, daemon=True).start()

    def on_exit(self, icon, item):
        self.running = False
        self.icon.stop()

    def update_icon(self):
        if not self.icon:
            return
        is_up = check_services_running()
        self.icon.icon = create_icon_image(is_up)
        status_text = "Bot Running 🟢" if is_up else "Bot Stopped 🔴"
        self.icon.title = f"Media Remote Downloader ({status_text})"

    def run_status_loop(self):
        while self.running:
            try:
                self.update_icon()
            except Exception:
                pass
            time.sleep(5)

    def run(self):
        is_up = check_services_running()
        menu = Menu(
            item('🟢 Bot Active' if is_up else '🔴 Bot Inactive', lambda icon, item: None, enabled=False),
            Menu.SEPARATOR,
            item('📁 Open Downloads Folder (U:\\movies)', self.on_open_folder),
            item('🌐 Open Jackett Indexer UI', self.on_open_jackett),
            item('⏬ Open qBittorrent UI', self.on_open_qbittorrent),
            item('📜 View Live Activity Logs', self.on_show_logs),
            Menu.SEPARATOR,
            item('⚙️ Start on Windows Boot', self.on_toggle_autostart, checked=lambda item: is_autostart_enabled()),
            item('🔄 Restart Services', self.on_restart_services),
            item('🛑 Stop / Start Services', self.on_toggle_services),
            Menu.SEPARATOR,
            item('❌ Exit', self.on_exit)
        )

        self.icon = pystray.Icon(
            APP_NAME,
            create_icon_image(is_up),
            f"Media Remote Downloader",
            menu=menu
        )

        threading.Thread(target=self.run_status_loop, daemon=True).start()
        self.icon.run()

if __name__ == '__main__':
    app = TrayApp()
    app.run()
