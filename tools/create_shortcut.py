"""Put a "Micronet e-Invoice" shortcut (with the company logo) on the desktop
and in the Start menu. It opens the app in its own window (einvoice.desktop).

The shortcut carries the same Windows AppUserModelID as the app window
(einvoice.desktop.APP_ID), so the taskbar shows the running app with this
shortcut's name and icon, and "Pin to taskbar" pins the app itself.

Run through "Create desktop shortcut.bat", which sets up .venv first.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "app.ico"
NAME = "Micronet e-Invoice"
APP_ID = "Micronet.eInvoice"  # keep in sync with einvoice/desktop.py

# Sets System.AppUserModel.ID on a .lnk file (no built-in PowerShell way).
SET_APP_ID_CS = r"""
using System;
using System.Runtime.InteropServices;
public static class LnkAppId {
  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    void GetCount(out uint c); void GetAt(uint i, out PropertyKey k); void GetValue(ref PropertyKey k, IntPtr v);
    void SetValue(ref PropertyKey k, ref PropVariant v); void Commit();
  }
  [ComImport, Guid("0000010b-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPersistFile {
    void GetClassID(out Guid g); [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string f, uint mode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string f, bool remember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string f); void GetCurFile(out IntPtr f);
  }
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class ShellLink {}
  [StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PropertyKey { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)] public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
  public static void Set(string lnk, string id) {
    var link = new ShellLink();
    ((IPersistFile)link).Load(lnk, 2 /* STGM_READWRITE */);
    var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    var value = new PropVariant { vt = 31 /* VT_LPWSTR */, p = Marshal.StringToCoTaskMemUni(id) };
    var store = (IPropertyStore)link;
    store.SetValue(ref key, ref value);
    store.Commit();
    ((IPersistFile)link).Save(lnk, true);
    Marshal.FreeCoTaskMem(value.p);
  }
}
"""


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
Add-Type -TypeDefinition @'
{SET_APP_ID_CS}
'@
[LnkAppId]::Set($path, '{APP_ID}')
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
