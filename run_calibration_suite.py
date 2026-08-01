#!/usr/bin/env python3
"""Launch the Temperature Calibration Suite.

Windows: double-click "Calibration Suite.pyw" - it opens the window with no
console behind it. Running this file with python.exe also works; the console
it creates is hidden automatically.

    pip install pyserial
    python run_calibration_suite.py

Works with the modules flat beside this file or inside a "calsuite" folder.
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

MODULES = ("transport", "formats", "adt286", "heatsource", "engine",
           "export", "theme", "datasync", "ui")


def hide_console():
    """Hide the console window on Windows, if we are the ones who made it.

    A console this process created is just noise behind the window. One the
    operator already had open is left alone, because hiding someone's
    terminal would be rude. Set CALSUITE_KEEP_CONSOLE=1 to disable this.

    The ctypes signatures below matter: GetConsoleWindow returns a 64-bit
    handle, and without an explicit restype ctypes truncates it to a C int,
    producing a bogus handle that ShowWindow silently ignores.
    """
    if sys.platform != "win32" or os.environ.get("CALSUITE_KEEP_CONSOLE"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        kernel32.GetConsoleWindow.restype = wintypes.HWND
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleProcessList.restype = wintypes.DWORD
        kernel32.GetConsoleProcessList.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

        window = kernel32.GetConsoleWindow()
        if not window:
            return False                # pythonw.exe: no console at all

        # How many programs share this console? Just us means we created it.
        size = 16
        buffer = (wintypes.DWORD * size)()
        count = kernel32.GetConsoleProcessList(buffer, size)
        if count == 0:                  # call failed; assume we own it
            count = 1
        if count > 1:
            return False                # started from someone's prompt

        SW_HIDE = 0
        user32.ShowWindow(window, SW_HIDE)
        return True
    except Exception:
        return False                    # never let cosmetics stop the app


def relaunch_windowless():
    """Restart under pythonw.exe so no console is ever created.

    The belt to hide_console's braces: if a console exists and we own it but
    hiding did not take, start the same script with the windowless
    interpreter and let this copy exit.
    """
    if sys.platform != "win32" or os.environ.get("CALSUITE_NO_RELAUNCH"):
        return False
    exe = sys.executable or ""
    if not exe or "pythonw" in os.path.basename(exe).lower():
        return False                    # already windowless
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pythonw):
        return False
    try:
        import subprocess
        env = dict(os.environ, CALSUITE_NO_RELAUNCH="1")
        DETACHED = 0x00000008
        NO_WINDOW = 0x08000000
        subprocess.Popen([pythonw, os.path.abspath(__file__)] + sys.argv[1:],
                         env=env, close_fds=True,
                         creationflags=DETACHED | NO_WINDOW)
        return True
    except Exception:
        return False


def ensure_package():
    """Make `import calsuite.x` work in both layouts."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    pkg_dir = os.path.join(HERE, "calsuite")
    if os.path.isdir(pkg_dir):
        missing = [m for m in MODULES
                   if not os.path.exists(os.path.join(pkg_dir, m + ".py"))]
        if missing:
            complain(pkg_dir, missing)
        return pkg_dir

    missing = [m for m in MODULES
               if not os.path.exists(os.path.join(HERE, m + ".py"))]
    if missing:
        complain(HERE, missing)
    pkg = types.ModuleType("calsuite")
    pkg.__path__ = [HERE]
    pkg.__version__ = "1.1.0"
    sys.modules["calsuite"] = pkg
    return HERE


_ensure_package = ensure_package        # kept for older callers


def alert(title, message):
    """Show a message even when there is no console to print to."""
    print(f"{title}\n\n{message}")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def complain(folder, missing):
    alert("Calibration Suite - files are missing",
          "Looked in:\n  " + folder + "\n\nMissing:\n  "
          + ", ".join(m + ".py" for m in missing)
          + "\n\nPut all of these next to the launcher (or in a folder "
            "called 'calsuite' beside it):\n  "
          + "  ".join(m + ".py" for m in MODULES))
    sys.exit(1)


def main():
    if not hide_console() and sys.platform == "win32":
        # A console exists that we could not hide. Start again windowless.
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.GetConsoleWindow.restype = wintypes.HWND
            still_there = bool(k.GetConsoleWindow())
        except Exception:
            still_there = False
        if still_there and relaunch_windowless():
            return
    ensure_package()
    try:
        from calsuite.ui import main as run_ui
    except ImportError as exc:
        if "tkinter" in str(exc).lower():
            alert("Calibration Suite - tkinter is missing",
                  "This Python was installed without tkinter, which the "
                  "window needs.\n\n"
                  "Windows/macOS: reinstall Python from python.org and keep "
                  "the 'tcl/tk and IDLE' option checked.\n"
                  "Debian/Ubuntu:  sudo apt install python3-tk\n"
                  "Fedora:         sudo dnf install python3-tkinter")
            sys.exit(1)
        raise
    except Exception as exc:
        alert("Calibration Suite - could not start", str(exc))
        raise
    run_ui()


if __name__ == "__main__":
    main()
