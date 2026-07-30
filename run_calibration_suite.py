#!/usr/bin/env python3
"""Launch the Temperature Calibration Suite.

    pip install pyserial
    python run_calibration_suite.py

Works with either layout:

    A) package layout                B) flat layout
       run_calibration_suite.py         run_calibration_suite.py
       calsuite/                        adt286.py
           __init__.py                  engine.py
           adt286.py                    export.py
           ...                          ...

If the modules sit flat beside this file, they are registered as the
"calsuite" package automatically, so nothing needs moving.
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

MODULES = ("transport", "formats", "adt286", "heatsource", "engine",
           "export", "ui")


def _ensure_package():
    """Make `import calsuite.x` work in both layouts."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)

    pkg_dir = os.path.join(HERE, "calsuite")
    if os.path.isdir(pkg_dir):
        missing = [m for m in MODULES
                   if not os.path.exists(os.path.join(pkg_dir, m + ".py"))]
        if missing:
            _complain(pkg_dir, missing)
        return pkg_dir

    # Flat layout: publish this directory as the calsuite package.
    missing = [m for m in MODULES
               if not os.path.exists(os.path.join(HERE, m + ".py"))]
    if missing:
        _complain(HERE, missing)
    pkg = types.ModuleType("calsuite")
    pkg.__path__ = [HERE]           # relative imports resolve against this
    pkg.__version__ = "1.0.0"
    sys.modules["calsuite"] = pkg
    return HERE


def _complain(folder, missing):
    print("Temperature Calibration Suite - some files are missing.\n")
    print(f"Looked in: {folder}")
    print("Missing:   " + ", ".join(m + ".py" for m in missing) + "\n")
    print("Put these files next to run_calibration_suite.py (or inside a\n"
          "folder called 'calsuite' beside it):\n")
    print("    " + "  ".join(m + ".py" for m in MODULES))
    sys.exit(1)


def main():
    _ensure_package()
    try:
        from calsuite.ui import main as run_ui
    except ImportError as e:
        if "tkinter" in str(e).lower():
            print("Python is installed without tkinter, which this app needs "
                  "for its window.\n\n"
                  "Windows/macOS: reinstall Python from python.org and keep\n"
                  "the 'tcl/tk and IDLE' option checked.\n"
                  "Debian/Ubuntu:  sudo apt install python3-tk\n"
                  "Fedora:         sudo dnf install python3-tkinter")
            sys.exit(1)
        raise
    run_ui()


if __name__ == "__main__":
    main()
