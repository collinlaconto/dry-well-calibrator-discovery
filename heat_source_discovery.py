#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Heat Source Discovery Tool  —  ADT286 companion (v2)
====================================================
Connect a laptop to any RS-232 heat source (dry-well, bath, furnace) and this
tool gathers everything needed to add it as a custom temperature source on the
Additel ADT286 — no hunting through manuals.

What it discovers automatically
-------------------------------
* Working baud rate and TERMINATOR (scans combinations with safe, read-only
  probes)
* Protocol family (Fluke/Hart SCPI wells, classic Hart Scientific serial,
  generic SCPI, or unknown)
* The five ADT286 command fields, each VERIFIED against the live instrument:
    - Set-point writing command      (tested with a small delta, then restored)
    - Set-point reading command
    - Value command (block temperature the 286 uses for stability)
    - Unit reading command (+ the actual unit token for the mapping table)
    - Terminator
* Model / serial number suggestions parsed from the identity reply

What the user is prompted for
-----------------------------
* Make of the heat source and its serial number (required before the sheet
  is generated; pre-filled from the instrument's identity reply when
  possible)
* Temperature range (pre-filled for common Fluke wells; always editable)

Output
------
A complete, evidence-backed entry sheet: every ADT286 Temperature Source
Management field, the unit mapping, the range, instrument identity, the raw
reply captured for each verified command, and a per-family checklist for the
heat-source side. Save as .txt or copy to the clipboard. Profiles persist to
a JSON library.

Safety
------
* Discovery probes are read-only queries.
* The set-point write test moves the set point by a small delta (default
  0.5°) and restores the original value. The heat/cool ENABLE command is
  never sent.

Requirements:  Python 3.8+ (tkinter included)  and  pyserial
               pip install pyserial
"""

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

try:
    import serial
    from serial.tools import list_ports
    SERIAL_OK = True
    SERIAL_IMPORT_ERROR = ""
except Exception as _e:
    SERIAL_OK = False
    SERIAL_IMPORT_ERROR = str(_e)

APP_TITLE = "Heat Source Discovery Tool — ADT286 companion"
STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "heat_source_profiles.json")

TERMINATORS = {"CRLF": "\r\n", "CR": "\r", "LF": "\n"}
BAUD_SCAN_ORDER = [9600, 19200, 38400, 4800, 2400, 1200, 57600, 115200]

# ---------------------------------------------------------------------------
# Protocol knowledge base — extend these tables to support more instruments.
# ---------------------------------------------------------------------------
# Read-only queries used to fingerprint an instrument during the scan.
FINGERPRINT_PROBES = ["*IDN?", "*VER", "t"]

# Candidate command batteries, tried in order until one verifies live.
SP_READ_CANDIDATES_SCPI = ["SOUR:SPO?", "SOUR:TEMP:SPO?", "SETP?", "SP?"]
SP_READ_CANDIDATES_CLASSIC = ["s"]
VALUE_CANDIDATES_SCPI = ["SOUR:SENS:DAT? TEMP", "SOUR:SENS:DATA?",
                         "MEAS:TEMP?"]
VALUE_CANDIDATES_CLASSIC = ["t"]
UNIT_CANDIDATES = ["UNIT:TEMP?", "u"]

# A verified set-point READ command implies its paired WRITE command.
WRITE_PAIRS = {
    "SOUR:SPO?": "SOUR:SPO {value}",
    "SOUR:TEMP:SPO?": "SOUR:TEMP:SPO {value}",
    "SETP?": "SETP {value}",
    "SP?": "SP {value}",
    "s": "s={value}",
}

FAMILIES = {
    "fluke_scpi": {
        "label": "Fluke/Hart SCPI metrology well (914X / 917X / 9190A)",
        "commands": {
            "sp_read": "SOUR:SPO?",
            "sp_write": "SOUR:SPO {value}",
            "value": "SOUR:SENS:DAT? TEMP",
            "unit": "UNIT:TEMP?",
        },
        "value_alts": ["SOUR:SENS:DATA?"],
        "enable": "OUTP:STAT 1",
        "password": "SYST:PASS:CEN 1234",
        "checklist": [
            "ECHO Off in the COMM menu (echo breaks the ADT286 parser)",
            "PRINT / serial-period streaming Off",
            "CONT ENABLE resets to Off at every power-up — turn it On, or "
            "put OUTP:STAT 1 in the ADT286 enable/init field",
            "If the set point is protected (SETPOINT PROT), disable it or "
            "use password command SYST:PASS:CEN 1234 (default)",
        ],
    },
    "fluke_bath": {
        "label": "Fluke 6109A/7109A Portable Calibration Bath (SCPI 1999.0)",
        "commands": {
            "sp_read": "SOUR:SPO?",
            "sp_write": "SOUR:SPO {value}",
            "value": "SOUR:SENS:DATA?",
            "unit": "UNIT:TEMP?",
        },
        "value_alts": ["SOUR:SENS:DAT? TEMP"],
        "enable": "OUTP:STAT 1",
        "password": "SYST:PASS:CEN 1234",
        "checklist": [
            "Temperature control (OUTP:STAT) is OFF after power-up and *RST "
            "— put OUTP:STAT 1 in the ADT286 enable/init field or enable it "
            "on the panel, or set points will not take effect",
            "Setup > Instrument > Remote > Termination: set to match the "
            "terminator entered on the 286 (replies are CR-only unless LF "
            "is enabled)",
            "Serial Monitor (auto-streaming) OFF — it defaults OFF at "
            "power-up",
            "RS-232 port is DTE: use a NULL-MODEM cable; 8-N-1, 1200-38400 "
            "baud. The USB device port also works as a virtual COM port "
            "(driver on the product CD)",
        ],
    },
    "hart_classic": {
        "label": "Hart Scientific classic serial (Micro-Bath 6102/7102/7103, "
                 "9100-series)",
        "commands": {
            "sp_read": "s",
            "sp_write": "s={value}",
            "value": "t",
            "unit": "u",
        },
        "enable": "",
        "password": "",
        "checklist": [
            "Duplex/echo setting: turn echo Off if replies repeat the command",
            "Sample period / auto-print Off so the line stays quiet",
            "Command syntax (t, s, s=, u) follows Hart serial convention — "
            "the live Discover verification is the proof on your unit",
        ],
    },
    "additel_well": {
        "label": "Additel dry well / bath (875 / 878 series)",
        "commands": {},
        "enable": "",
        "password": "",
        "checklist": [
            "CHECK FIRST: the ADT286 ships with native Additel heat-source "
            "drivers — look in its built-in temperature source list (and "
            "update 286 firmware) before building a custom profile at all",
            "Exact remote syntax is in 'Programming Commands for 878' at "
            "additel.com/productresources if probing leaves gaps",
            "Over USB, install the Additel USB driver so the instrument "
            "appears as a virtual COM port on the laptop",
        ],
    },
    "generic_scpi": {
        "label": "Generic SCPI instrument (commands found by probing)",
        "commands": {},
        "enable": "",
        "password": "",
        "checklist": [
            "Turn off any command echo and any automatic data streaming",
            "Confirm the verified value command reads the BLOCK/control "
            "temperature, not a reference-probe input",
        ],
    },
    "unknown": {
        "label": "Unknown protocol (manual entry with Terminal assist)",
        "commands": {},
        "enable": "",
        "password": "",
        "checklist": [
            "Use the Terminal tab with the instrument's manual to find "
            "working commands, then enter them on the Review tab",
        ],
    },
}

# Known-model knowledge base: token -> (family, low, high, kind).
# Ranges are in °C; confirm against the nameplate before relying on them.
KNOWN_MODELS = {
    "9190":    ("fluke_scpi",   -95, 140, "Ultra-Cool Field Metrology Well"),
    "9170":    ("fluke_scpi",   -45, 140, "Field Metrology Well"),
    "9171":    ("fluke_scpi",   -30, 155, "Field Metrology Well"),
    "9172":    ("fluke_scpi",    35, 425, "Field Metrology Well"),
    "9173":    ("fluke_scpi",    50, 700, "Metrology Well"),
    "9142":    ("fluke_scpi",   -25, 150, "Field Metrology Well"),
    "9143":    ("fluke_scpi",    33, 350, "Field Metrology Well"),
    "9144":    ("fluke_scpi",    50, 660, "Field Metrology Well"),
    "6109":    ("fluke_bath",    35, 250, "Portable Calibration Bath"),
    "7109":    ("fluke_bath",   -25, 140, "Portable Calibration Bath"),
    "878-160": ("additel_well", -40, 160, "Reference Dry Well"),
    "878-425": ("additel_well",  33, 425, "Reference Dry Well"),
    "878-700": ("additel_well",  33, 700, "Reference Dry Well"),
    "7102":    ("hart_classic",  -5, 125, "Micro-Bath"),
    "7103":    ("hart_classic", -30, 125, "Micro-Bath"),
    "6102":    ("hart_classic",  35, 200, "Micro-Bath"),
}


def model_info_for(text):
    """Match a model token inside free text (IDN model field, *VER reply,
    or user-entered model). Returns a dict or None."""
    t = (text or "").upper()
    for token, (family, lo, hi, kind) in KNOWN_MODELS.items():
        if token in t:
            return {"token": token, "family": family,
                    "lo": lo, "hi": hi, "kind": kind}
    return None


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def first_float(text):
    """First numeric value in an instrument reply, or None."""
    if not text:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    return float(m.group()) if m else None


def ascii_ratio(raw):
    """Fraction of printable ASCII in raw bytes — wrong-baud replies score low."""
    if not raw:
        return 0.0
    ok = sum(1 for b in raw if 32 <= b <= 126 or b in (10, 13, 9))
    return ok / len(raw)


def clean_token(line):
    """Strip 'label:' prefixes and quotes from a reply like  u: C  ->  C."""
    if not line:
        return ""
    t = line.strip().strip('"').strip("'")
    if ":" in t:
        t = t.split(":", 1)[1]
    return t.strip()


def suggest_unit_mapping(token):
    t = (token or "").strip().upper()
    if not t:
        return "(no unit token captured — map manually on the 286)"
    if t.startswith(("C", "°C", "CEL")):
        return f"'{token}'  ->  °C"
    if t.startswith(("F", "°F", "FAH")):
        return f"'{token}'  ->  °F"
    if t.startswith("K"):
        return f"'{token}'  ->  K"
    return f"'{token}'  ->  (map manually)"


def classify_family(idn, classic_reply):
    """Decide the protocol family from fingerprint results."""
    up = (idn or "").upper()
    if up:
        if "ADDITEL" in up:
            return "additel_well"
        if "HART" in up or "FLUKE" in up:
            return "fluke_scpi"
        return "generic_scpi"
    if classic_reply:
        return "hart_classic"
    return "unknown"


def parse_idn(idn):
    """Best-effort (make, model, serial) from a comma-separated *IDN? reply."""
    parts = [p.strip() for p in (idn or "").split(",")]
    make = parts[0] if len(parts) > 0 else ""
    model = parts[1] if len(parts) > 1 else ""
    sn = parts[2] if len(parts) > 2 else ""
    if make.upper() == "HART":
        make = "Fluke (Hart Scientific)"
    return make, model, sn


# ---------------------------------------------------------------------------
# Serial link
# ---------------------------------------------------------------------------
class SerialLink:
    def __init__(self, log_fn):
        self.ser = None
        self.log = log_fn                  # callable(tag, text)
        self.terminator = "\r\n"
        self.reply_timeout = 1.0

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud, terminator, reply_timeout=1.0,
             databits=8, parity="N", stopbits=1, quiet=False):
        self.close(quiet=True)
        self.terminator = terminator
        self.reply_timeout = reply_timeout
        self.ser = serial.Serial(
            port=port, baudrate=int(baud),
            bytesize=serial.SEVENBITS if int(databits) == 7 else serial.EIGHTBITS,
            parity={"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
                    "O": serial.PARITY_ODD}[parity],
            stopbits=serial.STOPBITS_TWO if int(stopbits) == 2
            else serial.STOPBITS_ONE,
            timeout=0.15, write_timeout=1.0)
        if not quiet:
            self.log("INFO", f"Port open: {port} @ {baud}, terminator "
                             f"{terminator!r}")

    def close(self, quiet=False):
        if self.is_open:
            try:
                self.ser.close()
                if not quiet:
                    self.log("INFO", "Port closed.")
            except Exception:
                pass
        self.ser = None

    def listen(self, duration):
        end = time.time() + duration
        buf = b""
        while time.time() < end:
            n = self.ser.in_waiting
            buf += self.ser.read(n if n else 1)
        return buf

    def raw_exchange(self, cmd, wait):
        """Send cmd, collect raw bytes for up to `wait` seconds."""
        self.ser.reset_input_buffer()
        wire = cmd + self.terminator
        self.ser.write(wire.encode("ascii", errors="replace"))
        self.ser.flush()
        deadline = time.time() + wait
        buf = b""
        while time.time() < deadline:
            n = self.ser.in_waiting
            chunk = self.ser.read(n if n else 1)
            if chunk:
                buf += chunk
                if buf.endswith(b"\n") or buf.endswith(b"\r"):
                    time.sleep(0.06)
                    if not self.ser.in_waiting:
                        break
        return buf

    def send(self, cmd, expect_reply=True, log_target=None):
        if not self.is_open:
            raise RuntimeError("Port is not open.")
        tgt = log_target or "discover"
        self.log("TX", repr(cmd + self.terminator), tgt)
        if not expect_reply:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + self.terminator).encode("ascii",
                                                          errors="replace"))
            self.ser.flush()
            time.sleep(0.15)
            junk = self.ser.read(self.ser.in_waiting or 0)
            if junk:
                self.log("RX", repr(junk.decode("ascii", errors="replace")),
                         tgt)
            return []
        raw = self.raw_exchange(cmd, self.reply_timeout)
        text = raw.decode("ascii", errors="replace")
        self.log("RX", repr(text) if text else "<no response>", tgt)
        return [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]

    def query(self, cmd, log_target=None):
        """Send a query; strip a command echo if present.

        Returns (lines, echo_detected)."""
        lines = self.send(cmd, expect_reply=True, log_target=log_target)
        echo = bool(lines) and lines[0].strip().upper() == cmd.strip().upper()
        if echo:
            lines = lines[1:]
        return lines, echo


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x780")
        self.minsize(940, 660)

        self.busy = False
        self.serial_lock = threading.Lock()
        self.log_queue = queue.Queue()
        self.link = SerialLink(self._log_ts)
        self.profiles = self._load_store()

        # Discovery session state
        self.session = self._blank_session()

        self._build_ui()
        self._refresh_ports()
        self._refresh_profile_list()
        self.after(80, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not SERIAL_OK:
            messagebox.showwarning(
                "pyserial is not installed",
                "Serial features are disabled.\n\nInstall with:\n\n"
                "    pip install pyserial\n\nthen restart. "
                f"(import error: {SERIAL_IMPORT_ERROR})")

    @staticmethod
    def _blank_session():
        return {
            "port": "", "baud": "", "terminator_name": "", "echo": False,
            "idn": "", "classic_reply": "", "family": "", "kind": "",
            "sp_read": "", "sp_write": "", "value": "", "unit": "",
            "enable": "", "password": "",
            "unit_token": "",
            "evidence": {},           # field -> (command, sample reply)
            "sp_write_verified": False,
            "verified_at": "",
        }

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        # Library sidebar ----------------------------------------------------
        lib = ttk.LabelFrame(main, text="Profile library")
        lib.pack(side="left", fill="y", padx=(0, 8))
        self.lst_profiles = tk.Listbox(lib, width=28, height=20,
                                       exportselection=False)
        self.lst_profiles.pack(fill="y", expand=True, padx=6, pady=6)
        self.lst_profiles.bind("<<ListboxSelect>>", self._on_profile_selected)
        ttk.Button(lib, text="Save current as profile",
                   command=self._save_profile).pack(fill="x", padx=6, pady=2)
        ttk.Button(lib, text="Delete selected",
                   command=self._delete_profile).pack(fill="x", padx=6, pady=2)
        ttk.Label(lib, text=f"Stored in\n{os.path.basename(STORE_FILE)}",
                  foreground="#666").pack(padx=6, pady=(6, 8))

        self.nb = ttk.Notebook(main)
        self.nb.pack(side="left", fill="both", expand=True)

        # --- Tab 1: Instrument ---------------------------------------------
        t1 = ttk.Frame(self.nb)
        self.nb.add(t1, text=" 1 · Instrument ")

        qp = ttk.LabelFrame(t1, text="Known-model quick-pick — pre-loads the "
                                     "format before you even connect")
        qp.pack(fill="x", padx=8, pady=(8, 0))
        self.qp_map = {}
        qp_vals = []
        for token, (famid, lo, hi, kind) in KNOWN_MODELS.items():
            make = "Additel" if token.startswith("878") else "Fluke"
            disp = f"{make} {token}   ({kind}, {lo} to {hi} °C)"
            self.qp_map[disp] = token
            qp_vals.append(disp)
        self.var_qp = tk.StringVar()
        ttk.Combobox(qp, textvariable=self.var_qp, values=qp_vals, width=54,
                     state="readonly").pack(side="left", padx=6, pady=6)
        ttk.Button(qp, text="Load format",
                   command=self._apply_quick_pick).pack(side="left", padx=6)

        frm = ttk.LabelFrame(t1, text="Instrument details (Make and Serial "
                                      "number are required for the sheet)")
        frm.pack(fill="x", padx=8, pady=8)
        self.var_make = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_sn = tk.StringVar()
        self.var_rmin = tk.StringVar()
        self.var_rmax = tk.StringVar()
        self.var_runit = tk.StringVar(value="°C")
        rows = [("Make *", self.var_make, 36),
                ("Model", self.var_model, 36),
                ("Serial number *", self.var_sn, 36),
                ("Range min", self.var_rmin, 12),
                ("Range max", self.var_rmax, 12)]
        for r, (lab, var, w) in enumerate(rows):
            ttk.Label(frm, text=lab).grid(row=r, column=0, sticky="e", **pad)
            ttk.Entry(frm, textvariable=var, width=w).grid(
                row=r, column=1, sticky="w", **pad)
        ttk.Label(frm, text="Range unit").grid(row=5, column=0, sticky="e",
                                               **pad)
        ttk.Combobox(frm, textvariable=self.var_runit, values=["°C", "°F"],
                     width=10, state="readonly").grid(row=5, column=1,
                                                      sticky="w", **pad)
        ttk.Label(t1, wraplength=760, foreground="#555", justify="left",
                  text=("Quick-pick a known model, or fill in what you know "
                        "and go to tab 2. Discovery pre-fills Model and "
                        "Serial number from the instrument's identity reply "
                        "when it can. Always confirm range against the "
                        "nameplate or manual, because the ADT286 rejects "
                        "set points outside it.")
                  ).pack(fill="x", padx=10, pady=4)

        # --- Tab 2: Connect & detect ---------------------------------------
        t2 = ttk.Frame(self.nb)
        self.nb.add(t2, text=" 2 · Connect ")
        bar = ttk.Frame(t2)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Port").pack(side="left")
        self.var_port = tk.StringVar()
        self.cbo_port = ttk.Combobox(bar, textvariable=self.var_port,
                                     width=30, state="readonly")
        self.cbo_port.pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self._refresh_ports)\
            .pack(side="left", padx=2)
        self.btn_auto = ttk.Button(bar, text="Auto-detect connection",
                                   command=self._start_autodetect)
        self.btn_auto.pack(side="left", padx=10)
        ttk.Button(bar, text="Disconnect", command=self._disconnect)\
            .pack(side="left", padx=2)
        self.lbl_conn = ttk.Label(t2, text="Not connected",
                                  foreground="#b00020")
        self.lbl_conn.pack(fill="x", padx=10)

        manual = ttk.LabelFrame(t2, text="Manual connection (only if "
                                         "auto-detect fails)")
        manual.pack(fill="x", padx=8, pady=6)
        self.var_mbaud = tk.StringVar(value="9600")
        self.var_mterm = tk.StringVar(value="CRLF")
        ttk.Label(manual, text="Baud").grid(row=0, column=0, **pad)
        ttk.Combobox(manual, textvariable=self.var_mbaud, width=8,
                     values=[str(b) for b in BAUD_SCAN_ORDER],
                     state="readonly").grid(row=0, column=1, **pad)
        ttk.Label(manual, text="Terminator").grid(row=0, column=2, **pad)
        ttk.Combobox(manual, textvariable=self.var_mterm, width=6,
                     values=list(TERMINATORS.keys()),
                     state="readonly").grid(row=0, column=3, **pad)
        ttk.Button(manual, text="Connect with these settings",
                   command=self._manual_connect).grid(row=0, column=4, **pad)
        ttk.Label(manual, foreground="#555",
                  text="Auto-detect assumes 8-N-1 (near-universal for this "
                       "equipment).").grid(row=1, column=0, columnspan=5,
                                           sticky="w", padx=6)

        self.log_connect = scrolledtext.ScrolledText(t2, height=16,
                                                     wrap="word")
        self.log_connect.pack(fill="both", expand=True, padx=8, pady=6)
        self._style_log(self.log_connect)

        # --- Tab 3: Discover -------------------------------------------------
        t3 = ttk.Frame(self.nb)
        self.nb.add(t3, text=" 3 · Discover ")
        dbar = ttk.Frame(t3)
        dbar.pack(fill="x", padx=8, pady=8)
        self.btn_discover = ttk.Button(dbar,
                                       text="Discover & verify commands",
                                       command=self._start_discovery)
        self.btn_discover.pack(side="left")
        ttk.Label(dbar, text="   Set-point test delta").pack(side="left")
        self.var_delta = tk.StringVar(value="0.5")
        ttk.Entry(dbar, textvariable=self.var_delta, width=6)\
            .pack(side="left", padx=4)
        self.var_restore = tk.BooleanVar(value=True)
        ttk.Checkbutton(dbar, text="Restore original set point",
                        variable=self.var_restore).pack(side="left", padx=8)
        self.var_use_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(dbar, text="Send password before write (protected "
                                   "set points)",
                        variable=self.var_use_pw).pack(side="left", padx=8)
        ttk.Label(t3, foreground="#555", wraplength=860, justify="left",
                  text=("Probes are read-only; the only write is the "
                        "set-point test (small delta, restored). The enable "
                        "command is never sent, so the well is not driven.")
                  ).pack(fill="x", padx=10)
        self.log_discover = scrolledtext.ScrolledText(t3, height=20,
                                                      wrap="word")
        self.log_discover.pack(fill="both", expand=True, padx=8, pady=6)
        self._style_log(self.log_discover)

        # --- Tab 4: Review & edit -------------------------------------------
        t4 = ttk.Frame(self.nb)
        self.nb.add(t4, text=" 4 · Review ")
        rev = ttk.LabelFrame(t4, text="Discovered values (editable — these "
                                      "feed the sheet)")
        rev.pack(fill="x", padx=8, pady=8)
        self.rev_vars = {}
        rev_rows = [("Terminator", "terminator_name", 10),
                    ("Set-point writing command", "sp_write", 40),
                    ("Set-point reading command", "sp_read", 40),
                    ("Value command", "value", 40),
                    ("Unit reading command", "unit", 40),
                    ("Unit token captured", "unit_token", 14),
                    ("Enable / init (optional)", "enable", 40),
                    ("Password (optional)", "password", 40)]
        for r, (lab, key, w) in enumerate(rev_rows):
            ttk.Label(rev, text=lab).grid(row=r, column=0, sticky="e", **pad)
            var = tk.StringVar()
            self.rev_vars[key] = var
            ttk.Entry(rev, textvariable=var, width=w).grid(
                row=r, column=1, sticky="w", **pad)
        ttk.Label(t4, foreground="#555", wraplength=860, justify="left",
                  text=("Anything discovery could not verify is left blank — "
                        "find it with the Terminal tab and type it here. "
                        "Use {value} in the writing command where the "
                        "number goes.")).pack(fill="x", padx=10)

        # --- Tab 5: Sheet -----------------------------------------------------
        t5 = ttk.Frame(self.nb)
        self.nb.add(t5, text=" 5 · Sheet ")
        sbar = ttk.Frame(t5)
        sbar.pack(fill="x", padx=8, pady=8)
        ttk.Button(sbar, text="Generate ADT286 entry sheet",
                   command=self._generate_sheet).pack(side="left")
        ttk.Button(sbar, text="Save as .txt",
                   command=self._save_sheet).pack(side="left", padx=6)
        ttk.Button(sbar, text="Copy to clipboard",
                   command=self._copy_sheet).pack(side="left")
        self.txt_sheet = scrolledtext.ScrolledText(t5, wrap="none",
                                                   font=("Consolas", 10))
        self.txt_sheet.pack(fill="both", expand=True, padx=8, pady=6)

        # --- Tab 6: Terminal --------------------------------------------------
        t6 = ttk.Frame(self.nb)
        self.nb.add(t6, text=" Terminal ")
        tb = ttk.Frame(t6)
        tb.pack(fill="x", padx=8, pady=8)
        self.var_cmd = tk.StringVar()
        ent = ttk.Entry(tb, textvariable=self.var_cmd)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ent.bind("<Return>", lambda e: self._terminal_send())
        ttk.Button(tb, text="Send", command=self._terminal_send)\
            .pack(side="left")
        ttk.Label(t6, foreground="#555",
                  text="Manual tester — uses the current connection. Ends "
                       "with '?' = expect a reply.").pack(fill="x", padx=10)
        self.log_terminal = scrolledtext.ScrolledText(t6, wrap="word")
        self.log_terminal.pack(fill="both", expand=True, padx=8, pady=6)
        self._style_log(self.log_terminal)

    @staticmethod
    def _style_log(widget):
        widget.tag_config("TX", foreground="#0b5cad")
        widget.tag_config("RX", foreground="#20603d")
        widget.tag_config("PASS", foreground="#1a7f37",
                          font=("TkDefaultFont", 9, "bold"))
        widget.tag_config("FAIL", foreground="#b00020",
                          font=("TkDefaultFont", 9, "bold"))
        widget.tag_config("WARN", foreground="#9a6700")
        widget.tag_config("INFO", foreground="#444")
        widget.tag_config("STEP", font=("TkDefaultFont", 9, "bold"))
        widget.configure(state="disabled")

    # ------------------------------------------------------------ logging --
    def _log_ts(self, tag, text, target="discover"):
        self.log_queue.put((target, tag, text))

    def _drain_log(self):
        try:
            while True:
                target, tag, text = self.log_queue.get_nowait()
                widget = {"connect": self.log_connect,
                          "terminal": self.log_terminal}.get(
                              target, self.log_discover)
                widget.configure(state="normal")
                widget.insert("end", f"[{tag}] {text}\n", tag)
                widget.see("end")
                widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    # ----------------------------------------------------------- storage ---
    def _load_store(self):
        if os.path.exists(STORE_FILE):
            try:
                with open(STORE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception as e:
                messagebox.showwarning("Profile library",
                                       f"Could not read {STORE_FILE}:\n{e}")
        return []

    def _save_store(self):
        try:
            with open(STORE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.profiles, f, indent=2)
        except Exception as e:
            messagebox.showerror("Profile library",
                                 f"Could not write {STORE_FILE}:\n{e}")

    def _refresh_profile_list(self):
        self.lst_profiles.delete(0, "end")
        for p in self.profiles:
            mark = " [verified]" if p.get("sp_write_verified") else ""
            self.lst_profiles.insert(
                "end", f"{p.get('make','?')} {p.get('model','')} "
                       f"SN {p.get('sn','?')}{mark}")

    def _current_profile(self):
        p = {
            "make": self.var_make.get().strip(),
            "model": self.var_model.get().strip(),
            "sn": self.var_sn.get().strip(),
            "range_min": self.var_rmin.get().strip(),
            "range_max": self.var_rmax.get().strip(),
            "range_unit": self.var_runit.get().strip(),
        }
        p.update({k: v.get().strip() for k, v in self.rev_vars.items()})
        for key in ("port", "baud", "echo", "idn", "family", "kind",
                    "evidence", "sp_write_verified", "verified_at"):
            p[key] = self.session.get(key)
        return p

    def _apply_profile(self, p):
        self.var_make.set(p.get("make", ""))
        self.var_model.set(p.get("model", ""))
        self.var_sn.set(p.get("sn", ""))
        self.var_rmin.set(p.get("range_min", ""))
        self.var_rmax.set(p.get("range_max", ""))
        self.var_runit.set(p.get("range_unit", "°C"))
        for k, var in self.rev_vars.items():
            var.set(p.get(k, ""))
        self.session = self._blank_session()
        for key in ("idn", "family", "kind", "evidence", "sp_write_verified",
                    "verified_at", "baud", "port", "echo"):
            if key in p:
                self.session[key] = p[key]
        self.session["terminator_name"] = p.get("terminator_name", "")

    def _on_profile_selected(self, _e=None):
        sel = self.lst_profiles.curselection()
        if sel:
            self._apply_profile(self.profiles[sel[0]])

    def _save_profile(self):
        p = self._current_profile()
        if not p["make"] or not p["sn"]:
            messagebox.showerror("Save profile",
                                 "Enter the Make and Serial number on tab 1 "
                                 "first.")
            return
        key = (p["make"], p["model"], p["sn"])
        for i, ex in enumerate(self.profiles):
            if (ex.get("make"), ex.get("model"), ex.get("sn")) == key:
                self.profiles[i] = p
                break
        else:
            self.profiles.append(p)
        self._save_store()
        self._refresh_profile_list()

    def _delete_profile(self):
        sel = self.lst_profiles.curselection()
        if sel and messagebox.askyesno("Delete profile", "Delete selected?"):
            del self.profiles[sel[0]]
            self._save_store()
            self._refresh_profile_list()

    # ------------------------------------------------------------ serial ---
    def _refresh_ports(self):
        if not SERIAL_OK:
            return
        ports = list(list_ports.comports())
        vals = [f"{p.device} — {p.description}" for p in ports]
        self.cbo_port["values"] = vals
        if vals and not self.var_port.get():
            self.var_port.set(vals[0])

    def _selected_port(self):
        raw = self.var_port.get()
        return raw.split(" — ")[0].strip() if raw else ""

    def _disconnect(self):
        with self.serial_lock:
            self.link.close()
        self.lbl_conn.configure(text="Not connected", foreground="#b00020")

    def _manual_connect(self):
        port = self._selected_port()
        if not SERIAL_OK or not port:
            messagebox.showerror("Connect", "Pick a COM port first.")
            return
        try:
            with self.serial_lock:
                self.link.open(port, self.var_mbaud.get(),
                               TERMINATORS[self.var_mterm.get()],
                               reply_timeout=1.0)
            self.session["port"] = port
            self.session["baud"] = self.var_mbaud.get()
            self.session["terminator_name"] = self.var_mterm.get()
            self.rev_vars["terminator_name"].set(self.var_mterm.get())
            self.lbl_conn.configure(
                text=f"Connected (manual): {port} @ {self.var_mbaud.get()} "
                     f"{self.var_mterm.get()}", foreground="#1a7f37")
        except Exception as e:
            messagebox.showerror("Connect", f"Could not open {port}:\n{e}")

    # ------------------------------------------------------ auto-detect ----
    def _start_autodetect(self):
        if self.busy:
            return
        port = self._selected_port()
        if not SERIAL_OK or not port:
            messagebox.showerror("Auto-detect", "Pick a COM port first "
                                                "(Refresh if empty).")
            return
        self.busy = True
        self.btn_auto.configure(state="disabled")
        threading.Thread(target=self._autodetect_worker, args=(port,),
                         daemon=True).start()

    def _autodetect_worker(self, port):
        log = lambda tag, txt: self._log_ts(tag, txt, "connect")
        found = None
        try:
            with self.serial_lock:
                log("STEP", f"Scanning {port}: baud × terminator with "
                            f"read-only probes {FINGERPRINT_PROBES} "
                            "(8-N-1)...")
                for baud in BAUD_SCAN_ORDER:
                    for tname, tchars in TERMINATORS.items():
                        try:
                            self.link.open(port, baud, tchars,
                                           reply_timeout=0.6, quiet=True)
                        except Exception as e:
                            log("FAIL", f"Cannot open {port}: {e}")
                            return
                        time.sleep(0.1)
                        hit = None
                        for probe in FINGERPRINT_PROBES:
                            raw = self.link.raw_exchange(probe, 0.55)
                            if raw and ascii_ratio(raw) >= 0.7:
                                hit = (probe,
                                       raw.decode("ascii", errors="replace"))
                                break
                            if raw:
                                log("INFO", f"{baud}/{tname}: garbled bytes "
                                            "(likely wrong baud)")
                                break
                        if hit:
                            found = (baud, tname, hit)
                            break
                        log("INFO", f"{baud}/{tname}: no reply")
                    if found:
                        break
                if not found:
                    log("FAIL", "No response at any combination. Check the "
                                "cable (null-modem vs straight), that "
                                "remote/serial mode is enabled on the "
                                "instrument, and the COM number; then use "
                                "manual settings from its manual.")
                    self.link.close(quiet=True)
                    return

                baud, tname, (probe, reply_text) = found
                self.link.terminator = TERMINATORS[tname]
                self.link.reply_timeout = 1.0
                lines = [ln.strip() for ln in
                         re.split(r"[\r\n]+", reply_text) if ln.strip()]
                echo = bool(lines) and lines[0].upper() == probe.upper()
                if echo:
                    lines = lines[1:]
                first = lines[0] if lines else ""
                log("PASS", f"Live at {baud} baud, terminator {tname}. "
                            f"Probe {probe!r} -> {first!r}")
                if echo:
                    log("WARN", "The instrument echoes commands. Turn echo "
                                "off on the instrument — the ADT286 will "
                                "not parse echoed replies.")

                s = self.session
                s.update({"port": port, "baud": str(baud),
                          "terminator_name": tname, "echo": echo})
                if probe == "*IDN?":
                    s["idn"] = first
                elif probe == "t":
                    s["classic_reply"] = first
                else:
                    s["idn"] = first
                # If classic probe hit, still try *IDN? once for identity.
                if not s["idn"]:
                    idn_lines, _ = self.link.query("*IDN?",
                                                   log_target="connect")
                    if idn_lines:
                        s["idn"] = idn_lines[0]
                s["family"] = classify_family(s["idn"], s["classic_reply"])
                make, model, sn = parse_idn(s["idn"])
                info = model_info_for(model) or \
                    model_info_for(s["classic_reply"]) or \
                    model_info_for(s["idn"])
                if info:
                    s["family"] = info["family"]
                    s["kind"] = info["kind"]
                    log("INFO", f"Recognized model token '{info['token']}' "
                                f"({info['kind']}, {info['lo']} to "
                                f"{info['hi']} °C).")
                fam = FAMILIES[s["family"]]
                log("INFO", f"Protocol family: {fam['label']}")

                self.after(0, self._prefill_instrument, make, model, sn)
                self.after(0, lambda: self.rev_vars["terminator_name"]
                           .set(tname))
                self.after(0, lambda: self.lbl_conn.configure(
                    text=f"Connected: {port} @ {baud} {tname} — "
                         f"{s['idn'] or fam['label']}",
                    foreground="#1a7f37"))
                log("STEP", "Connection detected. Go to tab 3 and run "
                            "Discover & verify commands.")
        finally:
            self.busy = False
            self.after(0, lambda: self.btn_auto.configure(state="normal"))

    def _prefill_instrument(self, make, model, sn):
        if make and not self.var_make.get().strip():
            self.var_make.set(make)
        if model and not self.var_model.get().strip():
            self.var_model.set(model)
        if sn and not self.var_sn.get().strip():
            self.var_sn.set(sn)
        info = model_info_for(model or self.var_model.get())
        if info and not self.var_rmin.get().strip():
            self.var_rmin.set(str(info["lo"]))
            self.var_rmax.set(str(info["hi"]))

    def _apply_quick_pick(self):
        token = self.qp_map.get(self.var_qp.get())
        if not token:
            return
        info = model_info_for(token)
        fam = FAMILIES[info["family"]]
        make = "Additel" if token.startswith("878") else "Fluke"
        self.var_make.set(make)
        self.var_model.set(token)
        self.var_rmin.set(str(info["lo"]))
        self.var_rmax.set(str(info["hi"]))
        self.var_runit.set("°C")
        s = self.session
        s["family"] = info["family"]
        s["kind"] = info["kind"]
        cmds = fam.get("commands") or {}
        for key, val in (("sp_write", cmds.get("sp_write", "")),
                         ("sp_read", cmds.get("sp_read", "")),
                         ("value", cmds.get("value", "")),
                         ("unit", cmds.get("unit", "")),
                         ("enable", fam.get("enable", "")),
                         ("password", fam.get("password", ""))):
            s[key] = val
            self.rev_vars[key].set(val)
        s["sp_write_verified"] = False
        self._log_ts("INFO", f"Pre-loaded format: {make} {token} — "
                             f"{fam['label']}. Connect (tab 2) and run "
                             "Discover (tab 3) to verify it against the "
                             "live instrument.")

    # -------------------------------------------------------- discovery ----
    def _start_discovery(self):
        if self.busy:
            return
        if not self.link.is_open:
            messagebox.showerror("Discover", "Connect on tab 2 first "
                                             "(Auto-detect or manual).")
            return
        self.busy = True
        self.btn_discover.configure(state="disabled")
        threading.Thread(target=self._discovery_worker, daemon=True).start()

    def _try_candidates(self, candidates, parser, field_label):
        """Try read-only candidates; return (cmd, parsed, sample) or None."""
        for cmd in candidates:
            lines, _ = self.link.query(cmd)
            sample = lines[0] if lines else ""
            parsed = parser(sample)
            if parsed is not None and parsed != "":
                self._log_ts("PASS", f"{field_label}: {cmd!r} works "
                                     f"(reply {sample!r})")
                return cmd, parsed, sample
            self._log_ts("INFO", f"{field_label}: {cmd!r} — no usable reply")
        self._log_ts("FAIL", f"{field_label}: no candidate verified. Find "
                             "it with the Terminal tab and enter it on the "
                             "Review tab.")
        return None

    def _discovery_worker(self):
        log = self._log_ts
        s = self.session
        try:
            with self.serial_lock:
                fam_id = s.get("family") or "unknown"
                fam = FAMILIES[fam_id]
                log("STEP", f"Discovery started "
                            f"({datetime.now().strftime('%H:%M:%S')}). "
                            f"Family: {fam['label']}")

                # Quiet-line check ---------------------------------------
                junk = self.link.listen(1.2)
                if junk:
                    log("WARN", f"Unsolicited data on the line: {junk!r} — "
                                "turn off auto-print/streaming on the "
                                "instrument, then re-run discovery.")

                # Build candidate order ----------------------------------
                pinned = fam.get("commands") or {}
                if pinned:
                    sp_read_c = [pinned["sp_read"]]
                    value_c = [pinned["value"]] + fam.get("value_alts", [])
                    unit_c = [pinned["unit"]]
                elif fam_id in ("generic_scpi", "additel_well"):
                    sp_read_c = SP_READ_CANDIDATES_SCPI + \
                        SP_READ_CANDIDATES_CLASSIC
                    value_c = VALUE_CANDIDATES_SCPI + VALUE_CANDIDATES_CLASSIC
                    unit_c = UNIT_CANDIDATES
                else:
                    sp_read_c = SP_READ_CANDIDATES_SCPI + \
                        SP_READ_CANDIDATES_CLASSIC
                    value_c = VALUE_CANDIDATES_CLASSIC + VALUE_CANDIDATES_SCPI
                    unit_c = UNIT_CANDIDATES

                # Set-point read -----------------------------------------
                log("STEP", "Set-point reading command")
                got = self._try_candidates(sp_read_c, first_float,
                                           "Set-point read")
                original_sp = None
                if got:
                    s["sp_read"], original_sp, sample = got
                    s["evidence"]["sp_read"] = (s["sp_read"], sample)

                # Value (block temperature) ------------------------------
                log("STEP", "Value command (block temperature)")
                gotv = self._try_candidates(value_c, first_float,
                                            "Value command")
                if gotv:
                    s["value"], val, sample = gotv
                    s["evidence"]["value"] = (s["value"], sample)
                    if fam_id in ("generic_scpi", "additel_well", "unknown"):
                        log("WARN", f"Reads {val}. Confirm this is the BLOCK "
                                    "temperature (what the 286 judges "
                                    "stability on), not a reference input.")

                # Unit ----------------------------------------------------
                log("STEP", "Unit reading command")
                gotu = self._try_candidates(unit_c, clean_token,
                                            "Unit read")
                if gotu:
                    s["unit"], token, sample = gotu
                    s["unit_token"] = token
                    s["evidence"]["unit"] = (s["unit"], sample)
                    log("INFO", f"Mapping suggestion: "
                                f"{suggest_unit_mapping(token)}")

                # Set-point write ----------------------------------------
                log("STEP", "Set-point writing command (small delta, "
                            "restored)")
                s["sp_write_verified"] = False
                if got is None:
                    log("FAIL", "Skipped — no working set-point read to "
                                "verify against.")
                else:
                    write_cmd = (pinned.get("sp_write")
                                 or WRITE_PAIRS.get(s["sp_read"]))
                    if not write_cmd:
                        log("FAIL", "No known write pairing for "
                                    f"{s['sp_read']!r}. Enter it manually "
                                    "on the Review tab.")
                    else:
                        try:
                            delta = float(self.var_delta.get())
                        except ValueError:
                            delta = 0.5
                        target = original_sp + delta
                        try:
                            lo = float(self.var_rmin.get())
                            hi = float(self.var_rmax.get())
                            if not (lo <= target <= hi):
                                target = original_sp - delta
                            if not (lo <= target <= hi):
                                target = original_sp
                        except ValueError:
                            pass
                        pw = fam.get("password") or s.get("password")
                        if self.var_use_pw.get() and pw:
                            self.link.send(pw, expect_reply=False)
                        if abs(target - original_sp) < 1e-9:
                            log("WARN", "Range too tight to test a delta; "
                                        "write command adopted from family "
                                        "knowledge but NOT verified.")
                            s["sp_write"] = write_cmd
                        else:
                            self.link.send(write_cmd.replace(
                                "{value}", f"{target:.2f}"),
                                expect_reply=False)
                            time.sleep(0.3)
                            rb_lines, _ = self.link.query(s["sp_read"])
                            rb = first_float(rb_lines[0]) if rb_lines else None
                            if rb is not None and abs(rb - target) <= 0.05:
                                s["sp_write"] = write_cmd
                                s["sp_write_verified"] = True
                                s["evidence"]["sp_write"] = (
                                    write_cmd,
                                    f"wrote {target:.2f}, read back {rb}")
                                log("PASS", f"Write verified: {write_cmd!r} "
                                            f"(wrote {target:.2f}, read "
                                            f"back {rb})")
                            else:
                                s["sp_write"] = write_cmd
                                log("FAIL", f"Wrote {target:.2f} but read "
                                            f"back {rb}. If unchanged, the "
                                            "set point is likely "
                                            "password-protected — tick the "
                                            "password box (Fluke) or check "
                                            "the instrument, then re-run.")
                            if self.var_restore.get():
                                self.link.send(write_cmd.replace(
                                    "{value}", f"{original_sp:.2f}"),
                                    expect_reply=False)
                                time.sleep(0.3)
                                chk, _ = self.link.query(s["sp_read"])
                                cv = first_float(chk[0]) if chk else None
                                if cv is not None and \
                                        abs(cv - original_sp) <= 0.05:
                                    log("PASS", f"Original set point "
                                                f"{original_sp} restored.")
                                else:
                                    log("WARN", f"Restore readback {cv} — "
                                                f"set it back to "
                                                f"{original_sp} manually.")

                # Family extras & wrap-up --------------------------------
                s["enable"] = fam.get("enable", "")
                s["password"] = fam.get("password", "")
                s["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                self.after(0, self._push_session_to_review)
                missing = [n for n, k in
                           [("set-point write", "sp_write"),
                            ("set-point read", "sp_read"),
                            ("value", "value"), ("unit", "unit")]
                           if not s.get(k)]
                log("STEP", "Discovery finished.")
                if missing:
                    log("WARN", "Unresolved fields: " + ", ".join(missing) +
                                ". Fill them on the Review tab (Terminal "
                                "helps).")
                else:
                    log("PASS", "All five ADT286 fields resolved. Check tab "
                                "4, then generate the sheet on tab 5.")
        except Exception as e:
            log("FAIL", f"Discovery stopped: {e}")
        finally:
            self.busy = False
            self.after(0, lambda: self.btn_discover.configure(state="normal"))

    def _push_session_to_review(self):
        s = self.session
        for key in ("terminator_name", "sp_write", "sp_read", "value",
                    "unit", "unit_token", "enable", "password"):
            self.rev_vars[key].set(s.get(key, "") or "")

    # ---------------------------------------------------------- terminal ---
    def _terminal_send(self):
        cmd = self.var_cmd.get().strip()
        if not cmd:
            return
        if not self.link.is_open:
            messagebox.showerror("Terminal", "Connect on tab 2 first.")
            return
        if self.busy:
            return

        def worker():
            with self.serial_lock:
                try:
                    self.link.send(cmd,
                                   expect_reply=cmd.rstrip().endswith("?")
                                   or len(cmd) <= 2,
                                   log_target="terminal")
                except Exception as e:
                    self._log_ts("FAIL", str(e), "terminal")
        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------- sheet --
    def _generate_sheet(self):
        p = self._current_profile()
        if not p["make"] or not p["sn"]:
            messagebox.showerror(
                "Instrument details required",
                "Enter the Make and Serial number of the heat source on "
                "tab 1 — the sheet records which physical instrument these "
                "values were verified against.")
            self.nb.select(0)
            return
        s = self.session
        fam = FAMILIES.get(s.get("family") or "unknown", FAMILIES["unknown"])
        verified = s.get("sp_write_verified")
        ev = s.get("evidence", {})
        L = []
        a = L.append
        a("=" * 76)
        a("ADT286 TEMPERATURE SOURCE — COMPLETE ENTRY SHEET")
        a(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        a("=" * 76)
        a("")
        a("INSTRUMENT")
        a("-" * 76)
        a(f"  Make ................... {p['make']}")
        a(f"  Model .................. {p['model'] or '(not entered)'}")
        if s.get("kind"):
            a(f"  Type ................... {s['kind']}")
        a(f"  Serial number .......... {p['sn']}")
        if s.get("idn"):
            a(f"  Identity reply ......... {s['idn']}")
        a(f"  Protocol family ........ {fam['label']}")
        if s.get("verified_at"):
            a(f"  Verified live .......... {s['verified_at']}"
              + ("" if verified else "  (write command NOT verified)"))
        a("")
        a("CONNECTION (heat source <-> ADT286 via USB-RS232 adapter)")
        a("-" * 76)
        a(f"  Baud rate .............. {s.get('baud') or '(set manually)'}")
        a("  Data bits / parity ..... 8 / None")
        a("  Stop bits .............. 1")
        a(f"  Terminator ............. {p['terminator_name'] or '(unknown)'}")
        if s.get("echo"):
            a("  !! Echo detected — turn echo OFF on the instrument before "
              "using the 286.")
        a("")
        a("ENTER IN: Temperature Source Management  ->  +  ->  custom source")
        a("-" * 76)
        a(f"  Source name ................. {p['make']} {p['model']} "
          f"SN {p['sn']}")
        a(f"  Range ....................... {p['range_min'] or '?'} to "
          f"{p['range_max'] or '?'} {p['range_unit']}")
        a(f"  Terminator .................. {p['terminator_name'] or '?'}")
        a(f"  Set-point writing command ... {p['sp_write'] or 'UNRESOLVED'}"
          "    ({value} = the number)")
        a(f"  Set-point reading command ... {p['sp_read'] or 'UNRESOLVED'}")
        a(f"  Value command ............... {p['value'] or 'UNRESOLVED'}")
        a(f"  Unit reading command ........ {p['unit'] or 'UNRESOLVED'}")
        a("  Unit mapping table .......... "
          + suggest_unit_mapping(p["unit_token"]))
        if p["enable"]:
            a(f"  Enable / init command ....... {p['enable']}")
        a("")
        a("COMMAND EVIDENCE (raw replies captured from this instrument)")
        a("-" * 76)
        if ev:
            for field, (cmd, sample) in ev.items():
                a(f"  {field:<9} {cmd!r:<28} -> {sample!r}")
        else:
            a("  (no live verification recorded — run Discover)")
        a("")
        a("HEAT-SOURCE-SIDE CHECKLIST (before every automated run)")
        a("-" * 76)
        for item in fam["checklist"]:
            a(f"  [ ] {item}")
        a(f"  [ ] Display unit matches the range unit ({p['range_unit']})")
        a("")
        a("ADT286-SIDE STEPS")
        a("-" * 76)
        a("  1. Plug the USB-RS232 adapter into a 286 USB-A port; RS-232 "
          "end to the heat source.")
        a("  2. Temperature Source Management -> + -> add a custom source; "
          "enter the values above.")
        a("  3. Use the 286's command communication tool to spot-check the "
          "set-point read command.")
        a("  4. Retrieve/connect the source; it is now selectable in the "
          "Probe Calibration app.")
        info = model_info_for(p["model"])
        if info and (p["range_min"], p["range_max"]) == \
                (str(info["lo"]), str(info["hi"])):
            a("")
            a("  NOTE: Range was pre-filled from a built-in hint table — "
              "confirm against the")
            a("  instrument nameplate or manual before relying on it.")
        text = "\n".join(L)
        self.txt_sheet.delete("1.0", "end")
        self.txt_sheet.insert("1.0", text)
        self.nb.select(4)

    def _save_sheet(self):
        text = self.txt_sheet.get("1.0", "end").strip()
        if not text:
            self._generate_sheet()
            text = self.txt_sheet.get("1.0", "end").strip()
            if not text:
                return
        base = re.sub(r"[^\w\-]+", "_",
                      f"{self.var_make.get()}_{self.var_model.get()}_"
                      f"{self.var_sn.get()}".strip("_") or "heat_source")
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"{base}_ADT286_entry_sheet.txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")

    def _copy_sheet(self):
        text = self.txt_sheet.get("1.0", "end").strip()
        if not text:
            self._generate_sheet()
            text = self.txt_sheet.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    # -------------------------------------------------------------- close --
    def _on_close(self):
        try:
            with self.serial_lock:
                self.link.close(quiet=True)
        finally:
            self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
