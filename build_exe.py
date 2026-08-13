import os
import sys
import subprocess
from PIL import Image, ImageDraw

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(PROJECT_DIR, "app_icon.ico")
EXE_OUTPUT_NAME = "MediaRemoteDownloader.exe"

def create_ico_file():
    print("Generating application icon...")
    img = Image.new('RGBA', (256, 256), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    
    # Outer dark rounded circle
    d.ellipse((16, 16, 240, 240), fill=(24, 28, 36, 255))
    
    # Movie Play Triangle Symbol
    d.polygon([(104, 80), (104, 176), (184, 128)], fill=(255, 255, 255, 255))
    
    # Status Indicator Light in Bottom-Right
    d.ellipse((168, 168, 232, 232), fill=(46, 204, 113, 255), outline=(24, 28, 36, 255), width=8)
    
    img.save(ICON_PATH, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
    print(f"Icon saved to: {ICON_PATH}")

def build_executable():
    create_ico_file()
    print("Building standalone executable with PyInstaller...")
    
    cmd = [
        "pyinstaller",
        "--noconsole",
        "--onefile",
        f"--icon={ICON_PATH}",
        "--name=MediaRemoteDownloader",
        "--clean",
        os.path.join(PROJECT_DIR, "tray_app.py")
    ]
    
    # Use python executable path if pyinstaller binary is not in PATH
    python_exe = sys.executable
    pyinstaller_cmd = [python_exe, "-m", "PyInstaller"] + cmd[1:]
    
    result = subprocess.run(pyinstaller_cmd, cwd=PROJECT_DIR)
    if result.returncode == 0:
        dist_exe = os.path.join(PROJECT_DIR, "dist", EXE_OUTPUT_NAME)
        target_exe = os.path.join(PROJECT_DIR, EXE_OUTPUT_NAME)
        if os.path.exists(dist_exe):
            if os.path.exists(target_exe):
                try:
                    os.remove(target_exe)
                except Exception:
                    pass
            import shutil
            shutil.copy(dist_exe, target_exe)
            print("====================================================")
            print(f"[SUCCESS] Executable created: {target_exe}")
            print("====================================================")
        else:
            print("[ERROR] Build completed but dist executable was not found.")
    else:
        print(f"[ERROR] PyInstaller build failed with exit code {result.returncode}")

if __name__ == '__main__':
    build_executable()
