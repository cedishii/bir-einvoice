"""Put a "Micronet e-Invoice" shortcut (with the company logo) on the desktop
and in the Start menu. It opens the app in its own window (einvoice.desktop).

Run through "Create desktop shortcut.bat", which sets up .venv first.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "app.ico"
NAME = "Micronet e-Invoice"


def make_icon() -> None:
    logo = Image.open(ROOT / "assets" / "logo.png").convert("RGBA")
    side = max(logo.size)
    square = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2), logo)
    square.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def create(folder_expr: str) -> str:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = f"""
$folder = {folder_expr}
$path = Join-Path $folder '{NAME}.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$s.TargetPath = '{pythonw}'
$s.Arguments = '-m einvoice.desktop'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = '{ICON},0'
$s.Description = 'Service Invoices and BIR e-invoices'
$s.Save()
$path
"""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main() -> int:
    if not (ROOT / ".venv" / "Scripts" / "pythonw.exe").exists():
        print("Run run.bat once first (it sets up .venv).")
        return 1
    make_icon()
    for where in ("[Environment]::GetFolderPath('Desktop')",
                  "[Environment]::GetFolderPath('Programs')"):
        print("Created", create(where))
    return 0


if __name__ == "__main__":
    sys.exit(main())
