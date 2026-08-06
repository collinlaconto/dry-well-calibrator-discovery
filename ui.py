"""Tkinter interface for the calibration suite."""

import json
import math
import os
import queue
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

from . import datasync, export, theme
from .adt286 import Adt286, DRIVER_REVISION
from .engine import (ChannelRegistry, RunEngine, default_profile,
                     tolerance_at, validate_profile, STATE_RUNNING)
from .formats import KNOWN_MODELS, profile_for_model
from .heatsource import HeatSource, checked_exchange
from .transport import (CANDIDATE_TCP_PORTS, DEFAULT_TCP_PORT,
                        SERIAL_OK, SERIAL_ERROR,
                        available_ports, describe_target, find_tcp_port,
                        normalize_target, target_is_set)

APP_TITLE = "Calibration Automation Suite"
APP_BUILD = "2026.08.06.2"
HERE = os.path.dirname(os.path.abspath(__file__))
# Profile libraries live beside run_calibration_suite.py. That is HERE's
# parent when the modules are in a calsuite/ folder, and HERE itself when
# they sit flat beside the launcher.
DATA_DIR = (os.path.dirname(HERE) if os.path.basename(HERE) == "calsuite"
            else HERE)
SOURCE_LIB = os.path.join(DATA_DIR, "heat_source_profiles.json")
RUN_LIB = os.path.join(DATA_DIR, "calibration_profiles.json")

PLOT_COLOURS = ["#0b5cad", "#b8500a", "#1a7f37", "#7a2fa8", "#b00020",
                "#0f766e", "#8a6d00", "#4b5563"]


def is_query_command(command):
    """Return whether the SCPI command header is a read-only query.

    Parameters follow the header, so ``SCAN:DATA:Last? 1`` is a query even
    though the full command string does not end with ``?``.
    """
    exact = str(command or "").strip()
    # Never let SCPI command chaining smuggle a write behind a query header.
    if any(separator in exact for separator in (";", "\r", "\n")):
        return False
    head = exact.split(None, 1)[0]
    return head.endswith("?")



# ------------------------------------------------------------ connections --
KIND_LABELS = {
    "serial": "USB / serial cable",
    "bluetooth": "Bluetooth (paired SPP port)",
    "tcp": "Network (Ethernet / Wi-Fi)",
}
LABEL_KINDS = {v: k for k, v in KIND_LABELS.items()}


class ConnectionPicker(ttk.Frame):
    """Chooses how to reach an instrument: cable, Bluetooth, or network."""

    def __init__(self, master, on_log=None, **kw):
        super().__init__(master, **kw)
        self.on_log = on_log or (lambda tag, msg: None)
        self.var_kind = tk.StringVar(value=KIND_LABELS["serial"])
        self.var_port = tk.StringVar()
        self.var_baud = tk.StringVar(value="9600")
        self.var_host = tk.StringVar()
        self.var_tcp = tk.StringVar(value="8000")
        self.busy = False

        ttk.Label(self, text="Connection").grid(row=0, column=0, sticky="e",
                                                padx=4, pady=3)
        ttk.Combobox(self, textvariable=self.var_kind, width=26,
                     state="readonly",
                     values=list(KIND_LABELS.values())).grid(
            row=0, column=1, sticky="w", padx=4, pady=3)
        self.var_kind.trace_add("write", lambda *a: self._sync())

        self.row_serial = ttk.Frame(self)
        self.row_serial.grid(row=1, column=0, columnspan=6, sticky="w")
        ttk.Label(self.row_serial, text="Port").pack(side="left", padx=4)
        self.cbo_port = ttk.Combobox(self.row_serial,
                                     textvariable=self.var_port, width=34,
                                     state="readonly")
        self.cbo_port.pack(side="left", padx=4)
        theme.Button(self.row_serial, text="Refresh",
                   command=self.refresh_ports).pack(side="left", padx=4)
        ttk.Label(self.row_serial, text="Baud").pack(side="left", padx=(12, 2))
        ttk.Combobox(self.row_serial, textvariable=self.var_baud, width=8,
                     state="readonly",
                     values=["1200", "2400", "4800", "9600", "19200", "38400",
                             "57600", "115200"]).pack(side="left")

        self.row_tcp = ttk.Frame(self)
        self.row_tcp.grid(row=2, column=0, columnspan=6, sticky="w")
        ttk.Label(self.row_tcp, text="Address").pack(side="left", padx=4)
        ttk.Entry(self.row_tcp, textvariable=self.var_host,
                  width=18).pack(side="left", padx=4)
        ttk.Label(self.row_tcp, text="Port").pack(side="left", padx=(12, 2))
        ttk.Entry(self.row_tcp, textvariable=self.var_tcp,
                  width=8).pack(side="left")
        theme.Button(self.row_tcp, text="Find port",
                   command=self.find_port).pack(side="left", padx=8)
        self.lbl_hint = ttk.Label(self, text="", style="Dim.TLabel",
                                  wraplength=620, justify="left")
        self.lbl_hint.grid(row=3, column=0, columnspan=6, sticky="w", padx=6)
        self.refresh_ports()
        self._sync()

    # -- state -------------------------------------------------------------
    @property
    def kind(self):
        return LABEL_KINDS.get(self.var_kind.get(), "serial")

    def _sync(self):
        kind = self.kind
        if kind == "tcp":
            self.row_serial.grid_remove()
            self.row_tcp.grid()
            self.lbl_hint.configure(
                text="Read the IP address off the instrument's network or "
                     "Wi-Fi screen. If you do not know the socket port, press "
                     "Find port and it will ask each likely port for its "
                     "identity.")
        else:
            self.row_tcp.grid_remove()
            self.row_serial.grid()
            if kind == "bluetooth":
                self.lbl_hint.configure(
                    text="Pair the instrument in Windows Bluetooth settings "
                         "first. Pairing creates an outgoing COM port — pick "
                         "that port here. (Instruments that only speak "
                         "Bluetooth Low Energy through Additel's app are not "
                         "reachable this way; use Wi-Fi or Ethernet.)")
            else:
                self.lbl_hint.configure(
                    text="For the ADT286 and Additel wells over USB, install "
                         "Additel's USB driver so the instrument appears as a "
                         "COM port.")

    def refresh_ports(self):
        vals = [f"{d} — {desc}" for d, desc in available_ports()]
        self.cbo_port["values"] = vals
        if vals and not self.var_port.get():
            self.var_port.set(vals[0])

    def get_target(self):
        kind = self.kind
        if kind == "tcp":
            try:
                port = int(str(self.var_tcp.get()).strip() or DEFAULT_TCP_PORT)
            except ValueError:
                port = DEFAULT_TCP_PORT
            return {"kind": "tcp", "host": self.var_host.get().strip(),
                    "tcp_port": port}
        raw = self.var_port.get()
        return {"kind": kind, "port": raw.split(" — ")[0].strip() if raw else "",
                "baud": self.var_baud.get() or "9600"}

    def set_target(self, target):
        t = normalize_target(target)
        self.var_kind.set(KIND_LABELS.get(t["kind"], KIND_LABELS["serial"]))
        if t["kind"] == "tcp":
            self.var_host.set(t.get("host", ""))
            self.var_tcp.set(str(t.get("tcp_port", DEFAULT_TCP_PORT)))
        else:
            if t.get("port"):
                match = [v for v in (self.cbo_port["values"] or [])
                         if v.startswith(t["port"])]
                self.var_port.set(match[0] if match else t["port"])
            self.var_baud.set(str(t.get("baud", "9600")))
        self._sync()

    # -- port discovery ----------------------------------------------------
    def find_port(self):
        host = self.var_host.get().strip()
        if not host:
            messagebox.showerror("Find port",
                                 "Enter the instrument's IP address first.")
            return
        if self.busy:
            return
        self.busy = True
        self.on_log("INFO", f"Asking {host} for its identity on "
                            f"{len(CANDIDATE_TCP_PORTS)} likely ports…")

        def work():
            found = find_tcp_port(
                host, progress=lambda p: self.on_log("INFO", f"  trying "
                                                             f"{host}:{p}"))
            self.after(0, lambda: self._found(host, found))
        threading.Thread(target=work, daemon=True).start()

    def _found(self, host, found):
        self.busy = False
        if not found:
            self.on_log("FAIL", f"No port on {host} answered *IDN?.")
            messagebox.showerror(
                "Nothing answered",
                f"No likely port on {host} replied to *IDN?.\n\n"
                "Check that the address is right and reachable (try pinging "
                "it), that remote/socket communication is switched on in the "
                "instrument's settings, and that a firewall is not blocking "
                "the connection. If Additel support gives you the port "
                "number, type it in directly.")
            return
        port, idn = found[0]
        self.var_tcp.set(str(port))
        self.on_log("PASS", f"{host}:{port} answered — {idn}")
        extra = ("" if len(found) == 1 else
                 f"\n\nOthers also answered: "
                 + ", ".join(str(p) for p, _ in found[1:]))
        messagebox.showinfo("Found it",
                            f"{host}:{port} identifies as:\n\n{idn}\n\n"
                            f"The port has been filled in." + extra)


# ---------------------------------------------------------------- plotting --
class PlotCanvas(tk.Canvas):
    """Minimal scatter/line plot — no third-party dependency."""

    def __init__(self, master, **kw):
        kw.setdefault("background", "white")
        kw.setdefault("height", 300)
        super().__init__(master, **kw)
        self.series = []
        self.x_label = ""
        self.y_label = ""
        self.title = ""
        self.bind("<Configure>", lambda e: self.redraw())

    def show(self, series, x_label="", y_label="", title=""):
        """series: [{'name', 'points': [(x, y)], 'colour'}]"""
        self.series = series or []
        self.x_label, self.y_label, self.title = x_label, y_label, title
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width() or 640
        h = self.winfo_height() or 300
        left, right, top, bottom = 74, 24, 34, 52
        pw, ph = max(w - left - right, 10), max(h - top - bottom, 10)

        pts = [p for s in self.series for p in s["points"]]
        if not pts:
            self.create_text(w / 2, h / 2, text="No results to plot yet",
                             fill="#888")
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        if xmax - xmin < 1e-9:
            xmin, xmax = xmin - 1, xmax + 1
        if ymax - ymin < 1e-9:
            pad = max(abs(ymax) * 0.1, 0.01)
            ymin, ymax = ymin - pad, ymax + pad
        ypad = (ymax - ymin) * 0.12
        ymin, ymax = ymin - ypad, ymax + ypad

        def sx(x):
            return left + (x - xmin) / (xmax - xmin) * pw

        def sy(y):
            return top + ph - (y - ymin) / (ymax - ymin) * ph

        # frame + grid
        self.create_rectangle(left, top, left + pw, top + ph, outline="#bbb")
        for i in range(5):
            yv = ymin + (ymax - ymin) * i / 4
            y = sy(yv)
            self.create_line(left, y, left + pw, y, fill="#eee")
            self.create_text(left - 6, y, text=f"{yv:.3f}", anchor="e",
                             fill="#444", font=("TkDefaultFont", 8))
        for i in range(5):
            xv = xmin + (xmax - xmin) * i / 4
            x = sx(xv)
            self.create_line(x, top, x, top + ph, fill="#eee")
            self.create_text(x, top + ph + 8, text=f"{xv:g}", anchor="n",
                             fill="#444", font=("TkDefaultFont", 8))
        if ymin < 0 < ymax:                     # zero-error reference line
            y0 = sy(0)
            self.create_line(left, y0, left + pw, y0, fill="#999", dash=(4, 3))

        if self.title:
            self.create_text(left + pw / 2, 14, text=self.title,
                             font=("TkDefaultFont", 10, "bold"))
        if self.x_label:
            self.create_text(left + pw / 2, h - 20, text=self.x_label,
                             fill="#444")
        if self.y_label:
            self.create_text(14, top + ph / 2, text=self.y_label, angle=90,
                             fill="#444")

        for i, s in enumerate(self.series):
            colour = s.get("colour") or PLOT_COLOURS[i % len(PLOT_COLOURS)]
            ordered = sorted(s["points"], key=lambda p: p[0])
            if len(ordered) > 1:
                flat = []
                for x, y in ordered:
                    flat += [sx(x), sy(y)]
                self.create_line(*flat, fill=colour, width=2, smooth=False)
            for x, y in ordered:
                cx, cy = sx(x), sy(y)
                self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                                 fill=colour, outline=colour)
            ly = top + 12 + i * 14
            self.create_line(left + pw - 66, ly, left + pw - 50, ly,
                             fill=colour, width=2)
            self.create_text(left + pw - 46, ly, text=s["name"], anchor="w",
                             fill="#333", font=("TkDefaultFont", 8))


# --------------------------------------------------------------------- app --
class SuiteApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} - build {APP_BUILD}")
        self.geometry("1280x860")
        self.minsize(1100, 740)
        self.fonts = theme.apply(self)

        self.adt = Adt286(logger=self._instrument_log)
        self.adt.on_recovery = lambda msg: self._log("WARN", msg)
        self.registry = ChannelRegistry()
        self.sources = {}            # name -> HeatSource
        self.run_profiles = self._load(RUN_LIB, [])
        self.source_profiles = self._load(SOURCE_LIB, [])
        self.engines = {}            # run_id -> RunEngine
        self.events = queue.Queue()
        self.log_queue = queue.Queue()
        self._run_seq = 0
        self.history_cycles = {}

        self._build_ui()
        self._log(
            "INFO", f"Calibration Suite build {APP_BUILD}; "
            f"driver {DRIVER_REVISION}; loaded from {HERE}")
        if ("downloads" in HERE.lower()
                and os.path.basename(HERE).lower().startswith("files (")):
            self._log(
                "WARN", "This copy is running from a generic Downloads "
                "folder. Confirm the build line above before testing; older "
                "folders with the same launcher name remain runnable.")
        self._refresh_ports()
        self._refresh_run_list()
        self._refresh_source_table()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if not SERIAL_OK:
            messagebox.showwarning(
                "pyserial is not installed",
                "Instrument communication is disabled.\n\n"
                "Install it with:\n    pip install pyserial\n\n"
                f"then restart. ({SERIAL_ERROR})")

    # ------------------------------------------------------------ storage --
    @staticmethod
    def _load(path, fallback):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return list(fallback)

    def _save(self, path, data):
        temporary = None
        try:
            folder = os.path.dirname(path) or "."
            fd, temporary = tempfile.mkstemp(prefix=".calsuite-", suffix=".tmp",
                                             dir=folder, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            temporary = None
        except Exception as e:
            messagebox.showerror("Saving failed", f"Could not write {path}:\n{e}")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # ------------------------------------------------------------ logging --
    def _instrument_log(self, tag, message):
        self.log_queue.put((tag, message))

    def _log(self, tag, message):
        self.log_queue.put((tag, message))

    def _drain(self):
        try:
            while True:
                tag, message = self.log_queue.get_nowait()
                stamp = datetime.now().strftime("%H:%M:%S")
                for widget in (self.log_main,):
                    widget.configure(state="normal")
                    widget.insert("end", f"{stamp} [{tag}] {message}\n", tag)
                    widget.see("end")
                    widget.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self._refresh_run_table()
        self._refresh_live_panel()
        try:
            health = self.adt.health
            colour = ("#b00020" if "no data" in health
                      else "#9a6700" if "recovered" in health else "#444")
            self.lbl_scan.configure(text=f"286 scan: {health}",
                                    foreground=colour)
            active = sum(1 for engine in self.engines.values()
                         if engine.is_active)
            self.nb.set_status(
                "DATA POLICY\nDevice readings retained unadjusted\n\n"
                f"ADT286\n{health}\n\nRUNS\n{active} active")
        except Exception:
            pass
        self.after(300, self._drain)

    def _handle_event(self, ev):
        kind = ev.get("kind")
        name = ev.get("name", "")
        if kind == "log":
            self._log(ev.get("tag", "INFO"), f"[{name}] {ev.get('message','')}")
        elif kind == "state":
            self._log("INFO", f"[{name}] run {ev.get('state')}")
            self._refresh_results_choices()
        elif kind == "result":
            r = ev.get("result")
            if r is not None:
                flag = "" if r.stable else "  (NOT STABLE)"
                self._log("PASS", f"[{name}] {r.setpoint:g}{r.unit} recorded"
                                  f"{flag}")
                self._refresh_results_choices()
                if self.var_result_run.get() in ("", name):
                    self.var_result_run.set(name)
                    self._show_results()

    # ----------------------------------------------------------------- UI --
    def _build_ui(self):
        self.nb = theme.PageStack(
            self, title="Calibration Suite", subtitle="Device data only",
            groups={0: "Calibration workflow", 4: "Records", 5: "Advanced"})
        self.nb.pack(fill="both", expand=True)
        self._build_instruments_tab()
        self._build_profiles_tab()
        self._build_runs_tab()
        self._build_results_tab()
        self._build_compare_tab()
        self._build_terminal_tab()
        self._build_activity_tab()
        self.nb.set_status("DATA POLICY\nDevice readings are retained unadjusted")


    def _build_activity_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text="Activity")
        head = ttk.Frame(t)
        head.pack(fill="x", padx=18, pady=(18, 8))
        ttk.Label(head, text="Activity", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, style="Dim.TLabel",
                  text="Every command sent and every reply received. Paste "
                       "this in when something looks wrong."
                  ).pack(anchor="w", pady=(2, 0))
        card = theme.Card(t)
        card.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.log_main = scrolledtext.ScrolledText(
            card, height=20, wrap="word", relief="flat", borderwidth=0,
            background="#111619", foreground="#d6dee0", insertbackground="#d6dee0",
            font=theme.FONTS.get("mono_small", ("Courier", 9)))
        self.log_main.pack(fill="both", expand=True)
        for tag, colour in (("TX", "#7fb3e8"), ("RX", "#8fd6ac"),
                            ("PASS", "#8fd6ac"), ("FAIL", "#f0908a"),
                            ("WARN", "#e8bd7a"), ("INFO", "#8e9a9e")):
            self.log_main.tag_config(tag, foreground=colour)
        self.log_main.configure(state="disabled")
        theme.Button(t, text="Copy everything",
                   command=self._copy_log).pack(anchor="e", padx=18,
                                                pady=(0, 16))

    def _copy_log(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.log_main.get("1.0", "end").strip())
            self._log("INFO", "Activity log copied to the clipboard.")
        except Exception:
            pass

    # --- tab 1 -------------------------------------------------------------
    def _build_instruments_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 1 · Instruments ")
        _h = ttk.Frame(t)
        _h.pack(fill="x", padx=18, pady=(18, 10))
        ttk.Label(_h, text="Instruments", style="Title.TLabel").pack(anchor="w")
        ttk.Label(_h, text="Connect the readout and every heat source, then prove their commands.", style="Dim.TLabel").pack(anchor="w",
                                                             pady=(2, 0))
        pad = {"padx": 6, "pady": 4}

        adt = ttk.LabelFrame(t, text="Additel ADT286 (USB)")
        adt.pack(fill="x", padx=18, pady=(0, 10))
        self.pick_adt = ConnectionPicker(adt, on_log=self._instrument_log)
        self.pick_adt.grid(row=0, column=0, columnspan=6, sticky="w", **pad)
        btns = ttk.Frame(adt)
        btns.grid(row=1, column=0, columnspan=6, sticky="w", padx=6)
        theme.Button(btns, text="Connect",
                   command=self._connect_adt).pack(side="left", padx=4)
        theme.Button(btns, text="Disconnect",
                   command=self._disconnect_adt).pack(side="left", padx=4)
        self.lbl_adt = ttk.Label(btns, text="Not connected",
                                 foreground=theme.RED)
        self.lbl_adt.pack(side="left", padx=10)
        ttk.Label(btns, text="   Read channels every").pack(side="left")
        self.var_poll = tk.StringVar(value="1")
        ttk.Combobox(btns, textvariable=self.var_poll, width=5,
                     state="readonly",
                     values=["1", "2", "5", "10"]).pack(side="left",
                                                               padx=4)
        ttk.Label(btns, text="s").pack(side="left")
        theme.Button(btns, text="Apply",
                   command=self._apply_poll).pack(side="left", padx=6)
        ttk.Label(adt, style="Dim.TLabel", wraplength=980, justify="left",
                  text=("Channels are read as the 286 has them configured. "
                        "The suite never changes the 286's channel setup or "
                        "units, because those are global and other runs "
                        "would be affected — set sensor types on the "
                        "instrument before starting. You can keep using the "
                        "286 by hand during a run: if changing its display "
                        "cancels the channel scan, the suite notices within a "
                        "few seconds and re-establishes it automatically.")
                  ).grid(row=2, column=0, columnspan=6, sticky="w", padx=8)
        self.lst_channels = tk.Listbox(adt, height=7, width=52)
        self.lst_channels.grid(row=3, column=0, columnspan=3, sticky="w",
                               padx=8, pady=6)
        self.lbl_channel_use = ttk.Label(adt, text="", justify="left",
                                         style="Dim.TLabel")
        self.lbl_channel_use.grid(row=3, column=3, columnspan=3, sticky="nw",
                                  padx=8, pady=6)

        hs = ttk.LabelFrame(t, text="Heat sources (each on its own "
                                    "connection)")
        hs.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        row = ttk.Frame(hs)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Add").pack(side="left")
        self.var_new_source = tk.StringVar()
        self.cbo_new_source = ttk.Combobox(row, textvariable=self.var_new_source,
                                           width=46, state="readonly")
        self.cbo_new_source.pack(side="left", padx=6)
        theme.Button(row, text="Connect heat source",
                   command=self._connect_source).pack(side="left", padx=6)
        theme.Button(row, text="Disconnect selected",
                   command=self._disconnect_source).pack(side="left")
        theme.Button(row, text="Check / discover commands",
                   command=self._verify_source).pack(side="left", padx=8)
        self.pick_source = ConnectionPicker(hs, on_log=self._instrument_log)
        self.pick_source.pack(fill="x", padx=6, pady=(0, 6))

        cols = ("name", "model", "port", "range", "status")
        self.tbl_sources = ttk.Treeview(hs, columns=cols, show="headings",
                                        height=8)
        for c, w, txt in (("name", 200, "Heat source"), ("model", 80, "Model"),
                          ("port", 180, "Connection"),
                          ("range", 140, "Range"), ("status", 220, "Status")):
            self.tbl_sources.heading(c, text=txt)
            self.tbl_sources.column(c, width=w, anchor="w")
        self.tbl_sources.pack(fill="both", expand=True, padx=6, pady=6)

    # --- tab 2 -------------------------------------------------------------
    def _build_profiles_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 2 · Profiles ")
        _h = ttk.Frame(t)
        _h.pack(fill="x", padx=18, pady=(18, 10))
        ttk.Label(_h, text="Calibration profiles", style="Title.TLabel").pack(anchor="w")
        ttk.Label(_h, text="What to calibrate, at which set points, and how steady it must be.", style="Dim.TLabel").pack(anchor="w",
                                                             pady=(2, 0))
        pad = {"padx": 6, "pady": 3}

        left = ttk.LabelFrame(t, text="Calibration profiles")
        left.pack(side="left", fill="y", padx=(18, 0), pady=(0, 16))
        self.lst_profiles = tk.Listbox(left, width=30, height=24,
                                       exportselection=False)
        self.lst_profiles.pack(fill="y", expand=True, padx=6, pady=6)
        self.lst_profiles.bind("<<ListboxSelect>>", self._select_profile)
        for txt, cmd in (("New", self._new_profile),
                         ("Save", self._save_profile),
                         ("Duplicate", self._duplicate_profile),
                         ("Delete", self._delete_profile)):
            theme.Button(left, text=txt, command=cmd).pack(fill="x", padx=6,
                                                         pady=2)

        form = ttk.Frame(t)
        form.pack(side="left", fill="both", expand=True, padx=18, pady=(0, 16))

        basic = ttk.LabelFrame(form, text="1 · Devices and channels")
        basic.pack(fill="x", pady=4)
        self.var_p_name = tk.StringVar()
        self.var_p_source = tk.StringVar()
        self.var_p_ref = tk.StringVar()
        ttk.Label(basic, text="Profile name").grid(row=0, column=0, sticky="e",
                                                   **pad)
        ttk.Entry(basic, textvariable=self.var_p_name, width=36).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Label(basic, text="Heat source").grid(row=1, column=0, sticky="e",
                                                  **pad)
        self.cbo_p_source = ttk.Combobox(basic, textvariable=self.var_p_source,
                                         width=34, state="readonly")
        self.cbo_p_source.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(basic, text="Reference probe channel").grid(
            row=2, column=0, sticky="e", **pad)
        self.cbo_p_ref = ttk.Combobox(basic, textvariable=self.var_p_ref,
                                      width=34, state="readonly")
        self.cbo_p_ref.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(basic, text="DUT channels").grid(row=3, column=0, sticky="ne",
                                                   **pad)
        self.lst_p_duts = tk.Listbox(basic, selectmode="multiple", height=7,
                                     width=34, exportselection=False)
        self.lst_p_duts.grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(basic, style="Dim.TLabel",
                  text="Ctrl-click to pick several. A channel can belong to "
                       "only one running calibration at a time.").grid(
            row=4, column=1, sticky="w", padx=6)

        sp = ttk.LabelFrame(form, text="2 · Test points")
        sp.pack(fill="x", pady=4)
        self.var_p_setpoints = tk.StringVar()
        ttk.Label(sp, text="Set points (°, comma separated)").grid(
            row=0, column=0, sticky="e", **pad)
        ttk.Entry(sp, textvariable=self.var_p_setpoints, width=52).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Label(sp, style="Dim.TLabel",
                  text="They run in the order given — e.g. 0, 50, 100, 50, 0 "
                       "for an up-and-down sequence.").grid(
            row=1, column=1, sticky="w", padx=6)

        tol = ttk.LabelFrame(form, text="3 · Tolerance — how far a device may sit "
                                       "from the reference and still pass")
        tol.pack(fill="x", pady=4)
        self.var_tol_mode = tk.StringVar(value="single")
        self.var_tol = tk.StringVar(value="0.05")
        self.var_tol_list = tk.StringVar(value="")
        ttk.Radiobutton(tol, text="One tolerance for the whole range",
                        variable=self.var_tol_mode, value="single",
                        command=self._sync_tolerance_mode).grid(
            row=0, column=0, sticky="w", **pad)
        self.ent_tol = ttk.Entry(tol, textvariable=self.var_tol, width=10)
        self.ent_tol.grid(row=0, column=1, sticky="w", **pad)
        ttk.Radiobutton(tol, text="A tolerance for each set point",
                        variable=self.var_tol_mode, value="per_point",
                        command=self._sync_tolerance_mode).grid(
            row=1, column=0, sticky="w", **pad)
        self.ent_tol_list = ttk.Entry(tol, textvariable=self.var_tol_list,
                                      width=42)
        self.ent_tol_list.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.lbl_tol_hint = ttk.Label(tol, style="Dim.TLabel", text="")
        self.lbl_tol_hint.grid(row=2, column=0, columnspan=3, sticky="w",
                               padx=6, pady=(0, 6))
        theme.Button(tol, text="Fill from the single value",
                   command=self._fill_tolerances).grid(row=1, column=3, **pad)

        stab = ttk.LabelFrame(form, text="4 · Stability and sampling")
        stab.pack(fill="x", pady=4)
        self.var_p_band = tk.StringVar(value="0.02")
        self.var_p_window = tk.StringVar(value="60")
        self.var_p_maxwait = tk.StringVar(value="2400")
        self.var_p_count = tk.StringVar(value="10")
        self.var_p_interval = tk.StringVar(value="5")
        self.var_p_soak = tk.StringVar(value="0")
        self.var_p_near = tk.BooleanVar(value=True)
        self.var_p_sptol = tk.StringVar(value="1.0")
        self.var_p_enable = tk.BooleanVar(value=True)
        self.var_p_disable = tk.BooleanVar(value=True)
        self.var_p_pw = tk.BooleanVar(value=False)
        self.var_p_timeout = tk.StringVar(value="record")
        grid = [
            ("Stability band (peak-to-peak)", self.var_p_band, 0, 0),
            ("Window it must hold (s)", self.var_p_window, 0, 2),
            ("Give up after (s)", self.var_p_maxwait, 1, 0),
            ("Soak after stable (s)", self.var_p_soak, 1, 2),
            ("Samples per set point", self.var_p_count, 2, 0),
            ("Seconds between samples", self.var_p_interval, 2, 2),
            ("Set-point tolerance", self.var_p_sptol, 3, 2),
        ]
        for label, var, r, c in grid:
            ttk.Label(stab, text=label).grid(row=r, column=c, sticky="e", **pad)
            ttk.Entry(stab, textvariable=var, width=10).grid(
                row=r, column=c + 1, sticky="w", **pad)
        ttk.Checkbutton(stab, text="Require reference near set point (required)",
                        variable=self.var_p_near, state="disabled").grid(
                            row=3, column=0, columnspan=2,
                            sticky="w", **pad)
        ttk.Checkbutton(stab, text="Enable heat source output at start",
                        variable=self.var_p_enable).grid(row=4, column=0,
                                                         columnspan=2,
                                                         sticky="w", **pad)
        ttk.Checkbutton(stab, text="Switch output off when finished",
                        variable=self.var_p_disable).grid(row=4, column=2,
                                                          columnspan=2,
                                                          sticky="w", **pad)
        ttk.Checkbutton(stab, text="Send password before set points",
                        variable=self.var_p_pw).grid(row=5, column=0,
                                                     columnspan=2, sticky="w",
                                                     **pad)
        ttk.Label(stab, text="If a point never stabilises").grid(
            row=5, column=2, sticky="e", **pad)
        ttk.Combobox(stab, textvariable=self.var_p_timeout, width=22,
                     state="readonly",
                     values=["record", "abort"]).grid(row=5, column=3,
                                                      sticky="w", **pad)

        act = ttk.LabelFrame(form, text="5 · Review and start")
        act.pack(fill="x", pady=6)
        theme.Button(act, text="Check this profile",
                   command=self._validate_profile).pack(side="left")
        theme.Button(act, text="Save and start run", style="Primary.TButton",
                   command=self._save_and_start).pack(side="left", padx=8)

    # --- tab 3 -------------------------------------------------------------
    def _build_runs_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 3 · Runs ")

        head = ttk.Frame(t)
        head.pack(fill="x", padx=18, pady=(18, 10))
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="Runs", style="Title.TLabel").pack(anchor="w")
        self.lbl_runcount = ttk.Label(titles, text="Nothing running",
                                      style="Dim.TLabel")
        self.lbl_runcount.pack(anchor="w", pady=(2, 0))
        self.lbl_scan = ttk.Label(head, text="", style="Dim.TLabel")
        self.lbl_scan.pack(side="right", pady=(6, 0))

        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=18, pady=(0, 12))
        self.var_run_choice = tk.StringVar()
        self.cbo_run_choice = ttk.Combobox(bar, textvariable=self.var_run_choice,
                                           width=32, state="readonly")
        self.cbo_run_choice.pack(side="left")
        theme.Button(bar, text="Start run", style="Primary.TButton",
                   command=self._start_selected).pack(side="left", padx=8)
        theme.Button(bar, text="Stop selected",
                   command=self._stop_selected).pack(side="left")
        theme.Button(bar, text="Stop everything", style="Danger.TButton",
                   command=self._stop_all).pack(side="right")

        # the run you are watching, at a size readable across the bench
        self.monitor = theme.MonitorCard(t)
        self.monitor.pack(fill="x", padx=18, pady=(0, 12))
        self.history = {}          # run_id -> deque of (t, reference)
        self._monitor_channels = None

        # every other run, one compact strip each
        self.rack = ttk.Frame(t)
        self.rack.pack(fill="x", padx=18)
        self.strips = {}
        self.selected_run = None
        self.lbl_empty = ttk.Label(
            t, style="Dim.TLabel",
            text="No calibrations running. Choose a profile above and start "
                 "one — you can run several at once, one per heat source.")
        self.lbl_empty.pack(anchor="w", padx=18, pady=8)

        live = ttk.Frame(t)
        live.pack(fill="both", expand=True, padx=18, pady=(14, 8))
        lh = ttk.Frame(live)
        lh.pack(fill="x")
        ttk.Label(lh, text="CHANNEL DETAIL", style="Dim.TLabel").pack(side="left")
        self.lbl_live_note = ttk.Label(lh, text="", style="Dim.TLabel")
        self.lbl_live_note.pack(side="right")
        lcols = ("channel", "role", "reading", "error", "age")
        self.tbl_live = ttk.Treeview(live, columns=lcols, show="headings",
                                     height=6)
        for c, w, txt in (("channel", 150, "Channel"), ("role", 150, "Role"),
                          ("reading", 140, "Reading"),
                          ("error", 160, "Error vs reference"),
                          ("age", 120, "Reading age")):
            self.tbl_live.heading(c, text=txt)
            self.tbl_live.column(c, width=w, anchor="w")
        self.tbl_live.pack(fill="both", expand=True, pady=(6, 0))

        ttk.Label(t, style="Dim.TLabel", wraplength=940, justify="left",
                  text=("All runs share the one 286: their channels are "
                        "scanned together and the readings fanned out, so a "
                        "channel belongs to one run while it is active.")
                  ).pack(fill="x", padx=18, pady=(0, 14))

    def _select_run(self, run_id):
        self.selected_run = run_id
        for rid, strip in self.strips.items():
            strip.set_selected(rid == run_id)
        self._refresh_live_panel()

    # --- tab 4 -------------------------------------------------------------
    def _build_results_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 4 · Results ")
        _h = ttk.Frame(t)
        _h.pack(fill="x", padx=18, pady=(18, 10))
        titles = ttk.Frame(_h)
        titles.pack(side="left")
        ttk.Label(titles, text="Results", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text="Device evidence and calculated calibration results are kept separate.",
            style="Dim.TLabel").pack(anchor="w", pady=(2, 0))
        self.lbl_result_status = ttk.Label(_h, text="NO RUN SELECTED",
                                           style="Invalid.TLabel")
        self.lbl_result_status.pack(side="right", pady=(4, 0))
        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=18, pady=(0, 8))
        ttk.Label(bar, text="Run").pack(side="left")
        self.var_result_run = tk.StringVar()
        self.cbo_result_run = ttk.Combobox(bar, textvariable=self.var_result_run,
                                           width=36, state="readonly")
        self.cbo_result_run.pack(side="left", padx=6)
        self.cbo_result_run.bind("<<ComboboxSelected>>",
                                 lambda e: self._show_results())
        theme.Button(bar, text="Refresh",
                   command=self._show_results).pack(side="left")
        theme.Button(bar, text="Export CSV",
                   command=self._export_csv).pack(side="left", padx=8)

        provenance = ttk.Frame(t)
        provenance.pack(fill="x", padx=18, pady=(0, 8))
        ttk.Label(provenance, text="DEVICE",
                  style="DeviceBadge.TLabel").pack(side="left")
        ttk.Label(
            provenance, style="Dim.TLabel",
            text=" Exact ADT286 tokens, scan cycle and acquisition time are read-only. "
        ).pack(side="left")
        ttk.Label(provenance, text="DERIVED",
                  style="DerivedBadge.TLabel").pack(side="left", padx=(10, 0))
        ttk.Label(
            provenance, style="Dim.TLabel",
            text=" Mean, sample SD, error and verdict only."
        ).pack(side="left")

        self.result_views = ttk.Notebook(t)
        self.result_views.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        derived = ttk.Frame(self.result_views)
        raw = ttk.Frame(self.result_views)
        self.result_views.add(derived, text="Derived summary")
        self.result_views.add(raw, text="Device readings · immutable")

        cols = ("setpoint", "stable", "ref", "refsd", "channel", "mean", "sd",
                "error", "tol", "verdict", "n")
        self.tbl_results = ttk.Treeview(derived, columns=cols, show="headings",
                                        height=8)
        for c, w, txt in (("setpoint", 90, "Set point"),
                          ("stable", 70, "Stable"),
                          ("ref", 120, "Reference"), ("refsd", 90, "Ref SD"),
                          ("channel", 110, "DUT channel"),
                          ("mean", 120, "DUT mean"), ("sd", 90, "DUT SD"),
                          ("error", 110, "Error"),
                          ("tol", 90, "Tolerance"), ("verdict", 70, "Result"),
                          ("n", 50, "n")):
            self.tbl_results.heading(c, text=txt)
            self.tbl_results.column(c, width=w, anchor="w")
        self.tbl_results.pack(fill="both", expand=True)
        self.tbl_results.tag_configure("fail", foreground=theme.RED)
        self.tbl_results.tag_configure("invalid", foreground=theme.AMBER)

        self.tbl_raw = ttk.Treeview(raw, columns=(), show="headings", height=8)
        raw_y = ttk.Scrollbar(raw, orient="vertical", command=self.tbl_raw.yview)
        raw_x = ttk.Scrollbar(raw, orient="horizontal", command=self.tbl_raw.xview)
        self.tbl_raw.configure(yscrollcommand=raw_y.set,
                               xscrollcommand=raw_x.set)
        self.tbl_raw.grid(row=0, column=0, sticky="nsew")
        raw_y.grid(row=0, column=1, sticky="ns")
        raw_x.grid(row=1, column=0, sticky="ew")
        raw.rowconfigure(0, weight=1)
        raw.columnconfigure(0, weight=1)

        self.plot = PlotCanvas(t, height=230)
        self.plot.pack(fill="both", expand=True, padx=18, pady=(0, 12))

    # --- tab 5: terminal ---------------------------------------------------
    def _build_compare_tab(self):
        """Compare data loggers against a reference probe."""
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 5 · Logger comparison ")
        _h = ttk.Frame(t)
        _h.pack(fill="x", padx=18, pady=(18, 10))
        ttk.Label(_h, text="Logger comparison",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(_h, style="Dim.TLabel", wraplength=880, justify="left",
                  text="Plot data loggers against a reference probe on one "
                       "time axis. Timestamp and temperature columns are found "
                       "automatically in almost any export."
                  ).pack(anchor="w", pady=(2, 0))

        missing = ([] if datasync.HAS_PANDAS else ["pandas"]) + \
                  ([] if datasync.HAS_PLOTLY else ["plotly"])
        if missing:
            note = theme.Panel(t)
            note.pack(fill="x", padx=18, pady=8)
            ttk.Label(note.inner, style="Card.TLabel", justify="left",
                      wraplength=760,
                      text=f"This tool needs {' and '.join(missing)}, which "
                           "are not installed:\n\n"
                           "    pip install pandas plotly openpyxl\n\n"
                           "Install them and restart. Everything else in the "
                           "application works without them."
                      ).pack(anchor="w")
            return

        # --- reference -----------------------------------------------------
        ref = ttk.LabelFrame(t, text="Reference")
        ref.pack(fill="x", padx=18, pady=(0, 8))
        pad = {"padx": 6, "pady": 4}
        self.var_ref_source = tk.StringVar(value="run")
        ttk.Radiobutton(ref, text="A calibration run from this application",
                        variable=self.var_ref_source, value="run",
                        command=self._sync_compare_source).grid(
            row=0, column=0, sticky="w", **pad)
        self.var_cmp_run = tk.StringVar()
        self.cbo_cmp_run = ttk.Combobox(ref, textvariable=self.var_cmp_run,
                                        width=34, state="readonly")
        self.cbo_cmp_run.grid(row=0, column=1, sticky="w", **pad)
        theme.Button(ref, text="Refresh",
                     command=self._refresh_compare_runs).grid(row=0, column=2,
                                                              **pad)
        ttk.Radiobutton(ref, text="A probe file", variable=self.var_ref_source,
                        value="file",
                        command=self._sync_compare_source).grid(
            row=1, column=0, sticky="w", **pad)
        self.var_probe_path = tk.StringVar()
        self.ent_probe = ttk.Entry(ref, textvariable=self.var_probe_path,
                                   width=42)
        self.ent_probe.grid(row=1, column=1, sticky="w", **pad)
        theme.Button(ref, text="Choose…",
                     command=self._pick_probe).grid(row=1, column=2, **pad)
        ttk.Label(ref, text="Unit").grid(row=1, column=3, sticky="e", **pad)
        self.var_probe_unit = tk.StringVar()
        self.cbo_probe_unit = ttk.Combobox(
            ref, textvariable=self.var_probe_unit, width=8, state="readonly",
            values=["", "°C", "°F", "K"])
        self.cbo_probe_unit.grid(row=1, column=4, sticky="w", **pad)
        self.lbl_probe_cols = ttk.Label(ref, style="Dim.TLabel", text="")
        self.lbl_probe_cols.grid(row=2, column=0, columnspan=3, sticky="w",
                                 padx=6, pady=(0, 4))
        # Always present: a logger can use elapsed time even when the
        # reference does not, and there was previously no way to say when it
        # started.
        ttk.Label(ref, text="Start time for elapsed-time files").grid(
            row=3, column=0, sticky="e", **pad)
        self.var_start = tk.StringVar()
        ttk.Entry(ref, textvariable=self.var_start, width=24).grid(
            row=3, column=1, sticky="w", **pad)
        ttk.Label(ref, style="Dim.TLabel",
                  text="YYYY-MM-DD HH:MM:SS — needed only by files that record "
                       "elapsed time rather than timestamps").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))

        # --- loggers -------------------------------------------------------
        dev = ttk.LabelFrame(t, text="Data loggers")
        dev.pack(fill="both", expand=True, padx=18, pady=8)
        bar = ttk.Frame(dev)
        bar.pack(fill="x", padx=6, pady=6)
        theme.Button(bar, text="Add files…",
                     command=self._add_logger_files).pack(side="left")
        theme.Button(bar, text="Remove selected",
                     command=self._remove_logger).pack(side="left", padx=8)
        theme.Button(bar, text="Clear",
                     command=self._clear_loggers).pack(side="left")
        cols = ("file", "time", "value", "unit", "rows")
        self.tbl_loggers = ttk.Treeview(dev, columns=cols, show="headings",
                                        height=6)
        for c, w, txt in (("file", 240, "File"), ("time", 175, "Time column"),
                          ("value", 175, "Temperature column"),
                          ("unit", 95, "Unit"), ("rows", 80, "Rows")):
            self.tbl_loggers.heading(c, text=txt)
            self.tbl_loggers.column(c, width=w, anchor="w")
        self.tbl_loggers.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.logger_files = {}          # iid -> {path, info, time, value}
        self.tbl_loggers.bind("<<TreeviewSelect>>",
                              lambda e: self._show_logger_columns())

        edit = ttk.Frame(dev)
        edit.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(edit, text="Time column").pack(side="left")
        self.var_log_time = tk.StringVar()
        self.cbo_log_time = ttk.Combobox(edit, textvariable=self.var_log_time,
                                         width=22, state="readonly")
        self.cbo_log_time.pack(side="left", padx=(4, 12))
        ttk.Label(edit, text="Temperature column").pack(side="left")
        self.var_log_value = tk.StringVar()
        self.cbo_log_value = ttk.Combobox(edit, textvariable=self.var_log_value,
                                          width=22, state="readonly")
        self.cbo_log_value.pack(side="left", padx=4)
        ttk.Label(edit, text="Unit").pack(side="left", padx=(12, 0))
        self.var_log_unit = tk.StringVar()
        ttk.Combobox(edit, textvariable=self.var_log_unit, width=12,
                     state="readonly",
                     values=["auto", "°C", "°F", "K"]).pack(side="left", padx=4)
        theme.Button(edit, text="Apply to selected",
                     command=self._apply_logger_columns).pack(side="left",
                                                              padx=8)
        ttk.Label(dev, style="Dim.TLabel", wraplength=880, justify="left",
                  text="Columns are detected automatically. Units are read "
                       "from headings or must be selected explicitly; they are "
                       "never guessed from the values. Display copies are "
                       "converted to °C for charting; source files are unchanged."
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        # --- output --------------------------------------------------------
        out = ttk.Frame(t)
        out.pack(fill="x", padx=18, pady=(0, 14))
        theme.Button(out, text="Build chart", style="Primary.TButton",
                     command=self._build_comparison).pack(side="left")
        self.lbl_compare = ttk.Label(out, style="Dim.TLabel", text="")
        self.lbl_compare.pack(side="left", padx=12)
        self._sync_compare_source()

    # ------------------------------------------------------ compare page ---
    def _sync_compare_source(self):
        from_run = self.var_ref_source.get() == "run"
        self.cbo_cmp_run.configure(state="readonly" if from_run else "disabled")
        self.ent_probe.configure(state="disabled" if from_run else "normal")
        self.cbo_probe_unit.configure(
            state="disabled" if from_run else "readonly")
        if from_run:
            self._refresh_compare_runs()

    def _refresh_compare_runs(self):
        labels = []
        for run_id, eng in self.engines.items():
            if eng.results:
                unit = dict(getattr(eng, "evidence", {}) or {}).get(
                    "readout_unit") or getattr(eng, "measurement_unit", "")
                labels.append(f"{eng.profile.get('name', '')} ({run_id}) — "
                              f"{datasync.sample_count(eng)} samples · "
                              f"{unit or 'unit not reported'}")
        self.cbo_cmp_run["values"] = labels
        if labels and self.var_cmp_run.get() not in labels:
            self.var_cmp_run.set(labels[-1])
        if not labels:
            self.lbl_probe_cols.configure(
                text="No finished calibration runs yet — run one, or choose a "
                     "probe file instead.")

    def _pick_probe(self):
        path = filedialog.askopenfilename(
            title="Choose the reference probe file",
            filetypes=[("Data files", "*.csv *.txt *.xlsx"), ("All", "*.*")])
        if not path:
            return
        self.var_probe_path.set(path)
        self.var_ref_source.set("file")
        try:
            info = datasync.analyze_file(path)
        except Exception as e:
            messagebox.showerror("Could not read the file", str(e))
            return
        self._probe_info = info
        self._probe_needs_start = info["needs_start"]
        unit = info.get("unit")
        self.var_probe_unit.set(
            {"C": "°C", "F": "°F", "K": "K"}.get(unit, ""))
        self.lbl_probe_cols.configure(
            text=f"Detected time '{info['time_guess']}', temperature "
                 f"'{info['value_guess']}', unit "
                 f"{self._unit_text(unit, info.get('unit_source'))}"
            + ("  ·  this file uses elapsed time, so a start time is needed"
               if info["needs_start"] else ""))
        if unit == "F":
            self._log("WARN", f"{os.path.basename(path)} is in °F — it will be "
                              "converted to °C.")
        self._sync_compare_source()

    def _add_logger_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose logger files",
            filetypes=[("Data files", "*.csv *.txt *.xlsx"), ("All", "*.*")])
        existing = {e["path"] for e in self.logger_files.values()}
        for path in paths:
            if path in existing:
                continue
            try:
                info = datasync.analyze_file(path)
            except Exception as e:
                self._log("FAIL", f"{os.path.basename(path)}: {e}")
                continue
            unit = info.get("unit")
            iid = self.tbl_loggers.insert(
                "", "end",
                values=(os.path.basename(path),
                        info["time_guess"] or "(not found)",
                        info["value_guess"] or "(not found)",
                        self._unit_text(unit, info.get("unit_source")),
                        len(info["df"])))
            self.logger_files[iid] = {"path": path, "info": info,
                                      "time": None, "value": None,
                                      "unit": None}
            self._log("INFO", f"{os.path.basename(path)}: time "
                              f"'{info['time_guess']}', temperature "
                              f"'{info['value_guess']}', unit "
                              f"{unit or 'not stated'}")
            if unit == "F":
                self._log("WARN", f"{os.path.basename(path)} is in °F — it "
                                  "will be converted to °C before charting.")
            elif not unit:
                self._log("WARN", f"{os.path.basename(path)}: no unit could be "
                                  "read from the heading. Select a unit before "
                                  "building the chart.")

    @staticmethod
    def _unit_text(unit, source):
        if not unit:
            return "not stated"
        label = {"C": "°C", "F": "°F", "K": "K"}.get(unit, unit)
        return label

    def _selected_logger(self):
        sel = self.tbl_loggers.selection()
        return self.logger_files.get(sel[0]) if sel else None

    def _show_logger_columns(self):
        entry = self._selected_logger()
        if not entry:
            return
        info = entry["info"]
        times = list(info["columns"])
        guess = info["time_guess"]
        if guess and guess not in times:
            times = [guess] + times
        self.cbo_log_time["values"] = times
        self.cbo_log_value["values"] = info["value_options"] or info["columns"]
        self.var_log_time.set(entry["time"] or guess or "")
        self.var_log_value.set(entry["value"] or info["value_guess"] or "")
        chosen = entry.get("unit")
        self.var_log_unit.set({"C": "°C", "F": "°F", "K": "K"}.get(chosen,
                                                                   "auto"))

    def _apply_logger_columns(self):
        sel = self.tbl_loggers.selection()
        entry = self._selected_logger()
        if not entry:
            messagebox.showinfo("Nothing selected",
                                "Select a file in the list first.")
            return
        entry["time"] = self.var_log_time.get().strip() or None
        entry["value"] = self.var_log_value.get().strip() or None
        picked = self.var_log_unit.get()
        entry["unit"] = {"°C": "C", "°F": "F", "K": "K"}.get(picked)
        # A different temperature column may well be in a different unit.
        info = entry["info"]
        shown_unit, source = entry["unit"], "name"
        if shown_unit is None:
            column = entry["value"] or info.get("value_guess")
            shown_unit = datasync.unit_from_name(column) if column else None
            source = "name" if shown_unit else None
        self.tbl_loggers.item(
            sel[0], values=(os.path.basename(entry["path"]),
                            entry["time"] or info["time_guess"],
                            entry["value"] or info["value_guess"],
                            self._unit_text(shown_unit, source),
                            len(info["df"])))

    def _remove_logger(self):
        for iid in self.tbl_loggers.selection():
            self.tbl_loggers.delete(iid)
            self.logger_files.pop(iid, None)

    def _clear_loggers(self):
        for iid in list(self.logger_files):
            self.tbl_loggers.delete(iid)
        self.logger_files.clear()

    def _build_comparison(self):
        from_run = self.var_ref_source.get() == "run"
        has_reference = (bool(self.var_cmp_run.get()) if from_run
                         else bool(self.var_probe_path.get().strip()))
        if not self.logger_files and not has_reference:
            messagebox.showinfo(
                "Nothing to chart",
                "Choose a reference, some logger files, or both.")
            return
        probe_series = None
        probe_path = None
        start = None

        if from_run and self.var_cmp_run.get():
            label = self.var_cmp_run.get()
            engine = next((e for rid, e in self.engines.items()
                           if f"({rid})" in label), None)
            if engine is None or not engine.results:
                messagebox.showerror(
                    "No run selected",
                    "Choose a calibration run with recorded results, or use a "
                    "probe file instead.")
                return
            try:
                probe_series = datasync.series_from_run(engine)
            except Exception as e:
                messagebox.showerror("Could not use that run", str(e))
                return
            source_unit = probe_series.attrs.get("source_unit")
            if source_unit and source_unit != "C":
                self._log(
                    "INFO", f"Calibration-run comparison: pinned °{source_unit} "
                    "device values are unchanged; a converted °C copy is used "
                    "only for this chart.")
            if probe_series.empty:
                messagebox.showerror(
                    "That run has no reference readings",
                    "The selected run recorded no samples on its reference "
                    "channel.")
                return
        elif not from_run:
            probe_path = self.var_probe_path.get().strip()
            if probe_path and not self.var_probe_unit.get():
                messagebox.showerror(
                    "Reference unit needed",
                    "The probe file does not state a temperature unit. Select "
                    "°C, °F, or K; the suite will not guess from its values.")
                return
            if probe_path and getattr(self, "_probe_needs_start", False):
                if not self.var_start.get().strip():
                    messagebox.showerror(
                        "Start time needed",
                        "This probe file records elapsed time, so it needs a "
                        "start date and time (YYYY-MM-DD HH:MM:SS).")
                    return

        text = self.var_start.get().strip()
        if text:
            try:
                start = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                messagebox.showerror(
                    "Start time not understood",
                    "Use the format YYYY-MM-DD HH:MM:SS, for example "
                    "2026-05-21 22:58:35.")
                return

        for entry in self.logger_files.values():
            selected_column = (entry.get("value") or
                               entry["info"].get("value_guess"))
            effective_unit = (entry.get("unit") or
                              datasync.unit_from_name(selected_column))
            if not effective_unit:
                messagebox.showerror(
                    "Logger unit needed",
                    f"{os.path.basename(entry['path'])} does not state a "
                    "temperature unit. Select the file, choose °C, °F, or K, "
                    "and apply it before building the chart.")
                return

        output = filedialog.asksaveasfilename(
            title="Save the chart", defaultextension=".html",
            initialfile="logger_comparison.html",
            filetypes=[("Web page", "*.html")])
        if not output:
            return

        self.lbl_compare.configure(text="Building…")

        def work():
            ok = self._compare_worker(probe_series, probe_path, start, output)
            self.after(0, lambda: self.lbl_compare.configure(
                text=f"Saved to {os.path.basename(output)}" if ok
                else "Could not build the chart — see Activity."))
            if ok:
                self.after(0, lambda: messagebox.showinfo(
                    "Chart ready", f"Saved:\n{output}"))
        threading.Thread(target=work, daemon=True).start()

    def _compare_worker(self, probe_series, probe_path, start, output):
        log = lambda message: self._log("INFO", message)
        try:
            if probe_series is None and probe_path:
                probe_unit = {"°C": "C", "°F": "F", "K": "K"}.get(
                    self.var_probe_unit.get())
                probe_series = datasync.load_any(probe_path, start=start,
                                                 unit=probe_unit, log_fn=log)
            frames, names = [], []
            for entry in self.logger_files.values():
                name = os.path.splitext(os.path.basename(entry["path"]))[0]
                log(f"Loading {name}")
                frames.append(datasync.load_any(
                    entry["path"], start=start, value_col=entry["value"],
                    time_col=entry["time"], log_fn=log,
                    unit=entry.get("unit")))
                names.append(name)
            if probe_series is not None and not probe_series.empty:
                first = probe_series["_abs_time"].min()
                last = probe_series["_abs_time"].max()
                log(f"Aligning to the reference window {first:%Y-%m-%d %H:%M:%S}"
                    f" to {last:%Y-%m-%d %H:%M:%S}")
                frames = [datasync.clip_to_window(f, n, first, last, log)
                          for f, n in zip(frames, names)]
                for frame, name in zip(frames, names):
                    if frame.empty:
                        self._log("WARN", f"{name} does not overlap the "
                                          "reference window at all.")
            datasync.build_chart(probe_series, frames, names, output)
            self._log("PASS", f"Chart written to {output}")
            return True
        except Exception as e:
            self._log("FAIL", f"Logger comparison failed: {e}")
            return False

    def _build_terminal_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 6 · Terminal ")
        _h = ttk.Frame(t)
        _h.pack(fill="x", padx=18, pady=(18, 10))
        ttk.Label(_h, text="Terminal", style="Title.TLabel").pack(anchor="w")
        ttk.Label(_h, text="Talk to an instrument directly, and find commands it accepts.", style="Dim.TLabel").pack(anchor="w",
                                                             pady=(2, 0))
        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Instrument").pack(side="left")
        self.var_term_target = tk.StringVar()
        self.cbo_term_target = ttk.Combobox(bar,
                                            textvariable=self.var_term_target,
                                            width=32, state="readonly")
        self.cbo_term_target.pack(side="left", padx=6)
        theme.Button(bar, text="Refresh list",
                   command=self._refresh_terminal_targets).pack(side="left")
        self.var_term_err = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Check SYSTem:ERRor? after every command",
                        variable=self.var_term_err).pack(side="left", padx=12)
        self.var_term_writes = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="Service mode: allow device-changing commands",
            variable=self.var_term_writes).pack(side="left", padx=8)

        row = ttk.Frame(t)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="Command").pack(side="left")
        self.var_term_cmd = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self.var_term_cmd)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self._terminal_send())
        theme.Button(row, text="Send",
                   command=self._terminal_send).pack(side="left")

        ttk.Label(t, style="Dim.TLabel", wraplength=1000, justify="left",
                  text=("Hand-test commands on any connected instrument, over "
                        "whichever connection it uses. Additel documents "
                        "SYSTem:ERRor? as the way to tell whether a command "
                        "that returns nothing was accepted: 0 means it was, "
                        "-110 means the instrument did not recognise the "
                        "command header. Queries end in '?'.")
                  ).pack(fill="x", padx=10, pady=(0, 4))

        quick = ttk.LabelFrame(t, text="This instrument's own commands")
        quick.pack(fill="x", padx=8, pady=4)
        theme.Button(quick, text="Read set point",
                   command=lambda: self._term_profile_cmd("sp_read")
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(quick, text="Read temperature",
                   command=lambda: self._term_profile_cmd("value")
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(quick, text="Read unit",
                   command=lambda: self._term_profile_cmd("unit")
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(quick, text="Identity",
                   command=lambda: (self.var_term_cmd.set("*IDN?"),
                                    self._terminal_send())
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(quick, text="Error queue",
                   command=lambda: (self.var_term_cmd.set("SYSTem:ERRor?"),
                                    self._terminal_send())
                   ).pack(side="left", padx=4, pady=4)

        find = ttk.LabelFrame(t, text="Find a command that this instrument "
                                      "accepts")
        find.pack(fill="x", padx=8, pady=4)
        theme.Button(find, text="Find temperature command",
                   command=lambda: self._term_sweep("value")
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(find, text="Find set-point command",
                   command=lambda: self._term_sweep("sp_read")
                   ).pack(side="left", padx=4, pady=4)
        theme.Button(find, text="Find unit command",
                   command=lambda: self._term_sweep("unit")
                   ).pack(side="left", padx=4, pady=4)
        ttk.Label(find, style="Dim.TLabel",
                  text="Tries every known form and keeps the one that answers."
                  ).pack(side="left", padx=8)

        self.log_terminal = scrolledtext.ScrolledText(t, height=18,
                                                      wrap="word")
        self.log_terminal.pack(fill="both", expand=True, padx=8, pady=6)
        for tag, colour in (("TX", "#0b5cad"), ("RX", "#20603d"),
                            ("PASS", "#1a7f37"), ("FAIL", "#b00020"),
                            ("WARN", "#9a6700"), ("INFO", "#444")):
            self.log_terminal.tag_config(tag, foreground=colour)
        self.log_terminal.configure(state="disabled")

    def _refresh_terminal_targets(self):
        names = (["ADT286"] if self.adt.is_open else []) + list(self.sources)
        self.cbo_term_target["values"] = names
        if names and self.var_term_target.get() not in names:
            self.var_term_target.set(names[0])

    def _term_link(self):
        """(link, lock, name) for the instrument selected on the terminal."""
        choice = self.var_term_target.get()
        if choice == "ADT286":
            if not self.adt.is_open:
                return (None, None, "ADT286")
            return (self.adt.link, self.adt.lock, "ADT286")
        source = self.sources.get(choice)
        if source is None or not source.is_open:
            return (None, None, choice)
        return (source.link, source.lock, source.name)

    def _term_log(self, tag, message):
        widget = self.log_terminal
        widget.configure(state="normal")
        widget.insert("end", f"[{tag}] {message}\n", tag)
        widget.see("end")
        widget.configure(state="disabled")

    def _term_profile_cmd(self, key):
        """Send whichever command this instrument actually uses."""
        choice = self.var_term_target.get()
        source = self.sources.get(choice)
        if source is None:
            if choice == "ADT286":
                messagebox.showinfo(
                    "Readings",
                    "The 286's readings come from its channel scan, not a "
                    "single command. Its live values are on the Runs tab; "
                    "channels are listed on the Instruments tab.")
            else:
                messagebox.showerror("Terminal",
                                     "Select a connected heat source first.")
            return
        cmd = (source.profile.get(key) or "").strip()
        if not cmd:
            labels = {"sp_read": "set point", "value": "temperature",
                      "unit": "unit"}
            messagebox.showinfo(
                "Not known yet",
                f"No {labels.get(key, key)} command is known for "
                f"{source.name} yet.\n\nUse 'Find {labels.get(key, key)} "
                "command' below and it will try every known form.")
            return
        self.var_term_cmd.set(cmd)
        self._terminal_send()

    def _term_sweep(self, kind):
        source = self.sources.get(self.var_term_target.get())
        if source is None or not source.is_open:
            messagebox.showerror("Terminal",
                                 "Select a connected heat source first.")
            return
        if any(engine.is_active and engine.heat_source is source
               for engine in self.engines.values()):
            messagebox.showerror(
                "Calibration in progress",
                f"{source.name} is part of an active calibration. Command "
                "discovery and profile changes are blocked until it stops.")
            return

        def work():
            try:
                result = source.sweep(kind, log=self._term_log)
            except Exception as e:
                self._term_log("FAIL", str(e))
                return
            if result["winner"]:
                self._save_source_profile(source)
                self.after(0, lambda: messagebox.showinfo(
                    "Found it",
                    f"{source.name} answers to:\n\n    "
                    f"{result['winner']}\n\nSaved to its profile."))
            else:
                self.after(0, lambda: messagebox.showinfo(
                    "Nothing worked",
                    "None of the known forms were accepted. The log lists "
                    "everything tried - type a command from Additel's "
                    "programming-commands PDF into the box above to test it."))
        threading.Thread(target=work, daemon=True).start()

    def _terminal_send(self):
        cmd = self.var_term_cmd.get().strip()
        if not cmd:
            return
        is_query = is_query_command(cmd)
        if not is_query and not self.var_term_writes.get():
            messagebox.showerror(
                "Query-only terminal",
                "Device-changing commands are blocked by default. Use a read "
                "query whose command header ends in '?', or explicitly enable "
                "Service mode.")
            return
        self._refresh_terminal_targets()
        link, lock, name = self._term_link()
        if link is None:
            messagebox.showerror(
                "Terminal",
                f"{name} is not connected. Connect it on the Instruments tab "
                "first.")
            return
        if not is_query:
            active = [engine for engine in self.engines.values()
                      if engine.is_active and
                      (name == "ADT286" or engine.heat_source.name == name)]
            if active:
                messagebox.showerror(
                    "Calibration in progress",
                    f"{name} is part of an active calibration. Device-changing "
                    "terminal commands are blocked until that run stops.")
                return
        check = bool(self.var_term_err.get())

        def work():
            try:
                with lock:
                    self._term_log("TX", f"{name}  <-  {cmd}")
                    reply, err = checked_exchange(
                        link, cmd, expect_reply=is_query, check_error=check)
                    self._term_log("RX", reply if reply
                                   else "(no reply)")
                    if err is not None:
                        if not err:
                            self._term_log("WARN",
                                           "SYSTem:ERRor? gave nothing - this "
                                           "instrument may not keep an error "
                                           "queue.")
                        elif err.strip().startswith("0"):
                            self._term_log("PASS", f"accepted ({err})")
                        else:
                            self._term_log("FAIL", f"rejected ({err})")
            except Exception as e:
                self._term_log("FAIL", str(e))
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- ports --
    def _refresh_ports(self):
        for picker in (getattr(self, "pick_adt", None),
                       getattr(self, "pick_source", None)):
            if picker is not None:
                picker.refresh_ports()
        choices = [p.get("name", "?") for p in self.source_profiles]
        choices += [f"[new] Fluke {t}" if not t.startswith("878")
                    else f"[new] Additel {t}" for t in KNOWN_MODELS]
        self.cbo_new_source["values"] = choices
        if choices and not self.var_new_source.get():
            self.var_new_source.set(choices[0])

    @staticmethod
    def _port_of(text):
        return text.split(" — ")[0].strip() if text else ""

    # --------------------------------------------------------------- ADT --
    def _connect_adt(self):
        if any(engine.is_active for engine in self.engines.values()):
            messagebox.showerror(
                "Runs in progress",
                "The readout connection cannot be changed while a calibration "
                "is running.")
            return
        if self.adt.is_open:
            messagebox.showinfo("ADT286", "The ADT286 is already connected.")
            return
        target = self.pick_adt.get_target()
        if not target_is_set(target):
            messagebox.showerror(
                "Connect",
                "Fill in how to reach the 286 first — a COM port for USB, or "
                "an address for a network connection.")
            return
        where = describe_target(target)

        def work():
            try:
                self.adt.connect(target)
                self.after(0, self._adt_connected)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "ADT286", f"Could not connect on {where}:\n{e}"))
        threading.Thread(target=work, daemon=True).start()

    def _adt_connected(self):
        self.lbl_adt.configure(
            text=f"Connected via {describe_target(self.adt.link.spec)} — "
                 f"{self.adt.idn} ({self.adt.unit})", foreground="#1a7f37")
        self.lst_channels.delete(0, "end")
        for c in self.adt.channels:
            self.lst_channels.insert("end", self.adt.describe(c))
        self._refresh_terminal_targets()
        self.cbo_p_ref["values"] = self.adt.channels
        self.lst_p_duts.delete(0, "end")
        for c in self.adt.channels:
            self.lst_p_duts.insert("end", c)

    def _apply_poll(self):
        """Change how often the 286 is scanned.

        Slower polling leaves the instrument freer for hands-on use during a
        run. It also sets the floor for the stability window, so the profile
        check is re-run against the new rate.
        """
        if any(engine.is_active for engine in self.engines.values()):
            messagebox.showerror(
                "Runs in progress",
                "The acquisition interval is locked while a calibration is "
                "running.")
            return
        try:
            value = float(str(self.var_poll.get()).replace(",", "."))
        except ValueError:
            messagebox.showerror("Read interval", "Enter a number of seconds.")
            return
        value = max(self.adt.minimum_poll_interval, value)
        self.adt.poll_interval = value
        self._log("INFO", f"The 286 will now be read every {value:g} s. "
                          f"Stability windows should be at least "
                          f"{3 * value:g} s.")
        for eng in self.engines.values():
            if eng.is_active:
                window = float(eng.profile.get("stability_window") or 0)
                if window and window < 3 * value:
                    self._log("WARN",
                              f"[{eng.profile.get('name')}] is running with a "
                              f"{window:g}s stability window, which is now "
                              f"too short for this read interval. Its points "
                              "may time out.")

    def _disconnect_adt(self):
        if any(e.is_active for e in self.engines.values()):
            messagebox.showerror("Runs in progress",
                                 "Stop the running calibrations before "
                                 "disconnecting the 286.")
            return
        self.adt.disconnect()
        self.lbl_adt.configure(text="Not connected", foreground="#b00020")
        self.lst_channels.delete(0, "end")

    # ------------------------------------------------------- heat sources --
    def _connect_source(self):
        choice = self.var_new_source.get()
        target = self.pick_source.get_target()
        if not choice:
            messagebox.showerror("Heat source", "Pick a heat source first.")
            return
        if not target_is_set(target):
            messagebox.showerror(
                "Heat source",
                "Fill in how to reach it — a COM port for a cable or a paired "
                "Bluetooth port, or an address for Ethernet/Wi-Fi.")
            return
        where = describe_target(target)
        if choice.startswith("[new] "):
            token = choice.rsplit(" ", 1)[-1]
            profile = profile_for_model(token)
            if not profile:
                messagebox.showerror("Heat source",
                                     f"No stored format for {token}.")
                return
        else:
            profile = next((dict(p) for p in self.source_profiles
                            if p.get("name") == choice), None)
            if not profile:
                messagebox.showerror("Heat source",
                                     "That profile is no longer available.")
                return
            profile.setdefault("range_unit", "°C")
        base = profile.get("name", "heat source")
        # A genuine duplicate is the same *connection*, not the same model
        # name -- two identical wells at different addresses are fine.
        for existing in self.sources.values():
            if existing.is_open and existing.connection == where:
                messagebox.showerror(
                    "Already connected",
                    f"{where} is already connected as '{existing.name}'.")
                return
        source = HeatSource(profile, logger=self._instrument_log)
        try:
            source.connect(target)
        except Exception as e:
            messagebox.showerror("Heat source",
                                 f"Could not connect {base} on {where}:\n{e}")
            return
        name = self._unique_source_name(base, source)
        if name != base:
            source.profile["name"] = name
            self._log("INFO", f"A heat source named '{base}' was already "
                              f"connected, so this one is '{name}'.")
        self.sources[name] = source
        self._refresh_source_table()
        self.cbo_p_source["values"] = list(self.sources)
        self._refresh_terminal_targets()
        if not self.var_p_source.get():
            self.var_p_source.set(name)
        for item in source.family_checklist():
            self._log("INFO", f"[{name}] check: {item}")

    def _verify_source(self):
        """Prove (or discover) the selected heat source's command set."""
        sel = self.tbl_sources.selection()
        if not sel:
            messagebox.showinfo("Check commands",
                                "Select a connected heat source in the table "
                                "first.")
            return
        name = self.tbl_sources.item(sel[0], "values")[0]
        source = self.sources.get(name)
        if source is None or not source.is_open:
            messagebox.showerror("Check commands",
                                 f"{name} is not connected.")
            return
        if any(engine.is_active and engine.heat_source is source
               for engine in self.engines.values()):
            messagebox.showerror(
                "Run in progress",
                "Command discovery can move the heat source set point. Stop "
                f"the calibration using {name} before running discovery.")
            return

        def work():
            try:
                report = source.verify_commands(log=self._instrument_log)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Check commands", f"{source.name}: {e}"))
                return
            self.after(0, lambda: self._verified(source, report))
        self._log("INFO", f"[{name}] checking commands over "
                          f"{source.connection}…")
        threading.Thread(target=work, daemon=True).start()

    def _verified(self, source, report):
        """Show what the instrument actually answered to."""
        name = source.name
        adopted = report.get("adopted", {})
        failed = report.get("failed", [])
        if adopted:
            self._save_source_profile(source)
            self._log("INFO", f"[{name}] saved to the profile library.")
        lines = [f"{name} — {report.get('idn') or 'no identity reply'}", ""]
        for key in ("sp_write", "sp_read", "value", "unit", "enable"):
            if key in adopted:
                lines.append(f"  {key:9s} {adopted[key]}")
        token = getattr(source, "unit_token", "")
        if token:
            from .formats import describe_unit_token
            lines.append(f"  {'unit id':9s} {describe_unit_token(token)}"
                         "   (sent automatically where the command needs it)")
        if failed:
            lines += ["", "Could not find: " + ", ".join(failed), "",
                      "Enter these by hand in heat_source_profiles.json, or "
                      "check Additel's 'Programming Commands' PDF for the "
                      "exact syntax. The Activity log shows everything that "
                      "was tried."]
        elif report.get("verified"):
            lines += ["", "Every command was proved on the instrument, "
                          "including the set-point write (tested with a small "
                          "change and restored)."]
        else:
            lines += ["", "Commands answered, but the set-point write could "
                          "not be confirmed by readback."]
        messagebox.showinfo("Command check", "\n".join(lines))

    def _save_source_profile(self, source):
        """Persist a heat source profile back to the shared library."""
        profile = dict(source.profile)
        for i, ex in enumerate(self.source_profiles):
            if ex.get("name") == profile.get("name"):
                self.source_profiles[i] = profile
                break
        else:
            self.source_profiles.append(profile)
        self._save(SOURCE_LIB, self.source_profiles)

    def _unique_source_name(self, base, source):
        """A stable, unique key for a connected heat source.

        Several identical wells are normal on a bench, so disambiguate by
        the instrument's own serial number where it reports one, and by the
        connection otherwise.
        """
        if base not in self.sources:
            return base
        serial = source.identity_serial or source.profile.get("sn") or ""
        for candidate in ([f"{base} SN {serial}"] if serial else []) + \
                         [f"{base} @ {source.connection}"]:
            if candidate not in self.sources:
                return candidate
        n = 2
        while f"{base} #{n}" in self.sources:
            n += 1
        return f"{base} #{n}"

    def _disconnect_source(self):
        sel = self.tbl_sources.selection()
        if not sel:
            return
        name = self.tbl_sources.item(sel[0], "values")[0]
        for eng in self.engines.values():
            if eng.is_active and eng.heat_source.name == name:
                messagebox.showerror("Run in progress",
                                     f"{name} is being used by a running "
                                     "calibration.")
                return
        source = self.sources.pop(name, None)
        if source:
            source.disconnect()
        self._refresh_source_table()
        self.cbo_p_source["values"] = list(self.sources)

    def _refresh_source_table(self):
        for row in self.tbl_sources.get_children():
            self.tbl_sources.delete(row)
        for name, s in self.sources.items():
            lo, hi = s.range
            rng = (f"{lo:g} to {hi:g} {s.unit}" if lo is not None
                   else "(not set)")
            busy = next((e.profile.get("name") for e in self.engines.values()
                         if e.is_active and e.heat_source.name == name), None)
            status = (f"running '{busy}'" if busy
                      else ("connected" if s.is_open else "disconnected"))
            self.tbl_sources.insert(
                "", "end",
                values=(name, s.profile.get("model", ""), s.connection, rng,
                        status))

    # ------------------------------------------------------------ profiles --
    def _refresh_run_list(self):
        self.lst_profiles.delete(0, "end")
        for p in self.run_profiles:
            self.lst_profiles.insert("end", p.get("name", "(unnamed)"))
        names = [p.get("name", "") for p in self.run_profiles]
        self.cbo_run_choice["values"] = names
        if names and not self.var_run_choice.get():
            self.var_run_choice.set(names[0])

    def _form_to_profile(self):
        p = default_profile(self.var_p_name.get().strip() or "New profile")
        input_errors = []
        p["heat_source"] = self.var_p_source.get()
        p["reference_channel"] = self.var_p_ref.get()
        p["dut_channels"] = [self.lst_p_duts.get(i)
                             for i in self.lst_p_duts.curselection()]
        p["setpoints"], bad = self._parse_numeric_list(
            self.var_p_setpoints.get())
        if bad:
            input_errors.append("Set points contain invalid value(s): "
                                + ", ".join(bad))
        for key, var, cast in (
                ("stability_band", self.var_p_band, float),
                ("stability_window", self.var_p_window, float),
                ("max_wait", self.var_p_maxwait, float),
                ("sample_count", self.var_p_count, int),
                ("sample_interval", self.var_p_interval, float),
                ("soak_seconds", self.var_p_soak, float),
                ("setpoint_tolerance", self.var_p_sptol, float)):
            try:
                value = cast(str(var.get()).strip().replace(",", "."))
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError
                p[key] = value
            except (TypeError, ValueError, OverflowError):
                input_errors.append(
                    f"{key.replace('_', ' ').title()} is not a valid number.")
        p["tolerance_mode"] = self.var_tol_mode.get()
        try:
            p["tolerance"] = float(str(self.var_tol.get()).replace(",", "."))
            if not math.isfinite(p["tolerance"]):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            input_errors.append("Tolerance is not a valid finite number.")
        p["tolerances"], bad = self._parse_numeric_list(
            self.var_tol_list.get())
        if bad:
            input_errors.append("Per-point tolerances contain invalid value(s): "
                                + ", ".join(bad))
        p["require_near_setpoint"] = bool(self.var_p_near.get())
        p["enable_output"] = bool(self.var_p_enable.get())
        p["disable_at_end"] = bool(self.var_p_disable.get())
        p["send_password"] = bool(self.var_p_pw.get())
        p["on_timeout"] = self.var_p_timeout.get()
        p["_input_errors"] = input_errors
        return p

    @staticmethod
    def _parse_setpoints(text):
        return SuiteApp._parse_numeric_list(text)[0]

    @staticmethod
    def _parse_numeric_list(text):
        out = []
        bad = []
        for chunk in (text or "").replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if any(character.isspace() for character in chunk):
                bad.append(chunk)
                continue
            try:
                value = float(chunk)
                if not math.isfinite(value):
                    raise ValueError
                out.append(value)
            except (ValueError, OverflowError):
                bad.append(chunk)
        return out, bad

    def _profile_to_form(self, p):
        self.var_p_name.set(p.get("name", ""))
        self.var_p_source.set(p.get("heat_source", ""))
        self.var_p_ref.set(p.get("reference_channel", ""))
        self.var_p_setpoints.set(", ".join(f"{s:g}"
                                           for s in p.get("setpoints", [])))
        self.var_p_band.set(str(p.get("stability_band", 0.02)))
        self.var_p_window.set(str(p.get("stability_window", 60)))
        self.var_p_maxwait.set(str(p.get("max_wait", 2400)))
        self.var_p_count.set(str(p.get("sample_count", 10)))
        self.var_p_interval.set(str(p.get("sample_interval", 5)))
        self.var_p_soak.set(str(p.get("soak_seconds", 0)))
        self.var_p_sptol.set(str(p.get("setpoint_tolerance", 1.0)))
        self.var_tol_mode.set(p.get("tolerance_mode", "single"))
        self.var_tol.set(str(p.get("tolerance", 0.05)))
        self.var_tol_list.set(", ".join(f"{v:g}"
                                        for v in p.get("tolerances", [])))
        self._sync_tolerance_mode()
        self.var_p_near.set(True)
        self.var_p_enable.set(bool(p.get("enable_output", True)))
        self.var_p_disable.set(bool(p.get("disable_at_end", True)))
        self.var_p_pw.set(bool(p.get("send_password")))
        self.var_p_timeout.set(p.get("on_timeout", "record"))
        wanted = set(p.get("dut_channels", []))
        self.lst_p_duts.selection_clear(0, "end")
        for i in range(self.lst_p_duts.size()):
            if self.lst_p_duts.get(i) in wanted:
                self.lst_p_duts.selection_set(i)

    def _sync_tolerance_mode(self, *_a):
        per_point = self.var_tol_mode.get() == "per_point"
        self.ent_tol.configure(state="disabled" if per_point else "normal")
        self.ent_tol_list.configure(state="normal" if per_point else "disabled")
        points = self._parse_setpoints(self.var_p_setpoints.get())
        if per_point:
            given = self._parse_setpoints(self.var_tol_list.get())
            self.lbl_tol_hint.configure(
                text=f"One value per set point, in the same order — "
                     f"{len(given)} given for {len(points)} set point(s).")
        else:
            self.lbl_tol_hint.configure(
                text="Applies at every set point. Devices outside it are "
                     "recorded and marked FAIL.")

    def _fill_tolerances(self):
        points = self._parse_setpoints(self.var_p_setpoints.get())
        value = (self.var_tol.get() or "0.05").strip()
        self.var_tol_list.set(", ".join([value] * len(points)))
        self.var_tol_mode.set("per_point")
        self._sync_tolerance_mode()

    def _select_profile(self, _e=None):
        sel = self.lst_profiles.curselection()
        if sel:
            self._profile_to_form(self.run_profiles[sel[0]])

    def _new_profile(self):
        self._profile_to_form(default_profile())
        self.var_p_name.set("")

    def _save_profile(self):
        p = self._form_to_profile()
        if p.get("_input_errors"):
            messagebox.showerror("Profile needs attention",
                                 "\n\n".join(p["_input_errors"]))
            return None
        p.pop("_input_errors", None)
        if not p["name"].strip():
            messagebox.showerror("Save", "Give the profile a name first.")
            return None
        for i, ex in enumerate(self.run_profiles):
            if ex.get("name") == p["name"]:
                self.run_profiles[i] = p
                break
        else:
            self.run_profiles.append(p)
        self._save(RUN_LIB, self.run_profiles)
        self._refresh_run_list()
        self._log("INFO", f"Profile '{p['name']}' saved.")
        return p

    def _duplicate_profile(self):
        p = self._form_to_profile()
        if p.get("_input_errors"):
            messagebox.showerror("Profile needs attention",
                                 "\n\n".join(p["_input_errors"]))
            return
        p.pop("_input_errors", None)
        p["name"] = p["name"] + " copy"
        self.run_profiles.append(p)
        self._save(RUN_LIB, self.run_profiles)
        self._refresh_run_list()
        self._profile_to_form(p)

    def _delete_profile(self):
        sel = self.lst_profiles.curselection()
        if sel and messagebox.askyesno(
                "Delete profile",
                f"Delete '{self.run_profiles[sel[0]].get('name')}'?"):
            del self.run_profiles[sel[0]]
            self._save(RUN_LIB, self.run_profiles)
            self._refresh_run_list()

    def _validate_profile(self, quiet=False):
        p = self._form_to_profile()
        hs = self.sources.get(p["heat_source"])
        problems = list(p.get("_input_errors", []))
        problems += validate_profile(
            p, hs, list(self.adt.channels) if self.adt.is_open else None,
            getattr(self.adt, "poll_interval", None)
            if self.adt.is_open else None,
            getattr(self.adt, "unit", "") if self.adt.is_open else None)
        busy = self.registry.in_use()
        for c in [p["reference_channel"]] + p["dut_channels"]:
            if c and c in busy:
                problems.append(f"{c} is in use by another running "
                                f"calibration.")
        if problems and not quiet:
            messagebox.showerror("Profile needs attention",
                                 "\n\n".join("• " + x for x in problems))
        elif not problems and not quiet:
            messagebox.showinfo(
                "Profile looks good",
                f"{len(p['setpoints'])} set point(s), "
                f"{len(p['dut_channels'])} device(s) under test, reference on "
                f"{p['reference_channel']}.")
        return problems

    def _save_and_start(self):
        if self._save_profile():
            self.var_run_choice.set(self.var_p_name.get().strip())
            self._start_selected()

    # ----------------------------------------------------------------- runs --
    def _start_selected(self):
        name = self.var_run_choice.get()
        profile = next((p for p in self.run_profiles
                        if p.get("name") == name), None)
        if not profile:
            messagebox.showerror("Start run", "Choose a saved profile first.")
            return
        if not self.adt.is_open:
            messagebox.showerror("Start run",
                                 "Connect the ADT286 on tab 1 first.")
            return
        hs = self.sources.get(profile.get("heat_source"))
        if hs is None:
            messagebox.showerror(
                "Start run",
                f"The heat source '{profile.get('heat_source')}' is not "
                "connected. Connect it on tab 1.")
            return
        for eng in self.engines.values():
            if eng.is_active and eng.profile.get("name") == name:
                messagebox.showerror("Start run", f"'{name}' is already "
                                                  "running.")
                return
            if eng.is_active and eng.heat_source.name == hs.name:
                messagebox.showerror(
                    "Heat source busy",
                    f"{hs.name} is already running "
                    f"'{eng.profile.get('name')}'. One calibration per heat "
                    "source at a time.")
                return
        self._run_seq += 1
        run_id = f"run{self._run_seq}"
        engine = RunEngine(run_id, profile, hs, self.adt, self.registry,
                           event_cb=self.events.put)
        try:
            engine.start()
        except Exception as e:
            messagebox.showerror("Could not start", str(e))
            return
        self.engines[run_id] = engine
        self._log("PASS", f"Started '{name}' on {hs.name}.")
        self._refresh_source_table()
        self._refresh_results_choices()

    def _selected_run_id(self):
        if self.selected_run in self.engines:
            return self.selected_run
        active = [rid for rid, e in self.engines.items() if e.is_active]
        return active[0] if len(active) == 1 else None

    def _stop_selected(self):
        run_id = self._selected_run_id()
        if not run_id or run_id not in self.engines:
            messagebox.showinfo("Stop run",
                                "Select a run in the table first.")
            return
        if not self.engines[run_id].stop():
            messagebox.showinfo("Stop run", "That calibration is not running.")
            return
        self._log("WARN", f"Stopping '{self.engines[run_id].profile.get('name')}'…")

    def _stop_all(self):
        active = [e for e in self.engines.values() if e.is_active]
        if not active:
            return
        for e in active:
            e.stop()
        self._log("WARN", f"Stopping {len(active)} run(s); outputs will be "
                          "switched off where the profile allows.")

    def _live(self, channel):
        """(value, age_seconds) for a channel from the shared scan."""
        try:
            reading = self.adt.latest(channel)
        except Exception:
            return (None, None)
        if reading is None or reading.temperature is None:
            return (None, None)
        return (reading.temperature, max(0.0, time.time() - reading.timestamp))

    def _display_frame(self, engine):
        """Values from one real scan cycle, or the run's last stored sample."""
        channels = ([engine.profile.get("reference_channel", "")] +
                    list(engine.profile.get("dut_channels", [])))
        if engine.is_active:
            reference = self.adt.latest(channels[0]) if channels[0] else None
            if reference is None:
                return {}, None, None
            frame = self.adt.snapshot(channels, cycle=reference.cycle)
            values = {channel: reading.temperature
                      for channel, reading in frame.items()
                      if reading is not None and reading.temperature is not None}
            return values, reference.timestamp, reference.cycle
        if engine.results and engine.results[-1].samples:
            sample = engine.results[-1].samples[-1]
            values = {channels[0]: sample.get("ref")}
            values.update(dict(sample.get("duts", {})))
            return values, sample.get("t"), sample.get("cycle")
        return {}, None, None

    def _refresh_live_panel(self):
        """Reference and DUT readings for the selected (or only) run."""
        for row in self.tbl_live.get_children():
            self.tbl_live.delete(row)
        run_id = self._selected_run_id()
        engine = self.engines.get(run_id)
        if engine is None:
            active = [e for e in self.engines.values() if e.is_active]
            engine = active[0] if len(active) == 1 else None
        if engine is None:
            self.lbl_live_note.configure(
                text="Select a run above to see its channels.")
            return
        unit = dict(getattr(engine, "evidence", {}) or {}).get(
            "readout_unit") or "(unit not reported)"
        ref_ch = engine.profile.get("reference_channel", "")
        values, acquired, _cycle = self._display_frame(engine)
        ref_value = values.get(ref_ch)
        ref_age = (None if acquired is None else
                   max(0.0, time.time() - acquired))
        rows = [(ref_ch, "reference probe", ref_value, None, ref_age)]
        for ch in engine.profile.get("dut_channels", []):
            value = values.get(ch)
            age = ref_age if value is not None else None
            error = (None if value is None or ref_value is None
                     else value - ref_value)
            rows.append((ch, "device under test", value, error, age))
        stale = False
        for channel, role, value, error, age in rows:
            if age is not None and age > 10:
                stale = True
            self.tbl_live.insert(
                "", "end",
                values=(channel, role,
                        "—" if value is None else f"{value:.3f} {unit}",
                        "" if error is None else f"{error:+.3f} {unit}",
                        "—" if age is None else f"{age:.0f} s ago"))
        name = engine.profile.get("name", "")
        if stale and engine.is_active:
            self.lbl_live_note.configure(
                text=f"{name}: readings are going stale — the 286's scan may "
                     "have been interrupted. It is being re-established "
                     "automatically.")
        elif engine.is_active:
            self.lbl_live_note.configure(
                text=f"{name}: live from the shared scan, updated every "
                     f"{getattr(self.adt, 'poll_interval', 1):g} s.")
        else:
            self.lbl_live_note.configure(
                text=f"{name}: last immutable device sample from this run.")

    def _refresh_run_table(self):
        """Keep one rack strip per run, in sync with the engines."""
        phase_kind = {
            "waiting for stability": "wait", "setting set point": "wait",
            "soaking": "wait", "sampling": "run", "finished": "done",
            "idle": "idle",
        }
        for run_id, eng in self.engines.items():
            strip = self.strips.get(run_id)
            if strip is None:
                strip = theme.RunStrip(self.rack, on_select=self._select_run,
                                       run_id=run_id)
                strip.pack(fill="x", pady=(0, 10))
                strip.set_channels(eng.profile.get("dut_channels", []),
                                   self._tolerance_for(eng))
                self.strips[run_id] = strip
                if self.selected_run is None:
                    self._select_run(run_id)

            kind = phase_kind.get(eng.phase, "run")
            if eng.state in ("aborted", "error"):
                kind = "bad"
            elif eng.state == "complete":
                kind = "done"
            phase = eng.phase if eng.is_active else eng.state
            strip.update_head(eng.profile.get("name", ""),
                              f"{eng.heat_source.name} · ref "
                              f"{eng.profile.get('reference_channel', '')}",
                              phase, kind)

            unit = dict(getattr(eng, "evidence", {}) or {}).get(
                "readout_unit") or "(unit not reported)"
            frame_values, _acquired, _cycle = self._display_frame(eng)
            ref_value = frame_values.get(
                eng.profile.get("reference_channel"))
            points = eng.profile.get("setpoints", []) or [1]
            done = len(eng.results)
            strip.update_values(
                "—" if ref_value is None else f"{ref_value:.4f}",
                "—" if eng.current_setpoint is None
                else f"{eng.current_setpoint:g} {unit}",
                self._flat_text(eng),
                done / max(len(points), 1),
                f"{done} of {len(points)} points"
                + (f" · next {points[done]:g} {unit}"
                   if done < len(points) and eng.is_active else ""))

            tol = self._tolerance_for(eng)
            for channel in eng.profile.get("dut_channels", []):
                value = frame_values.get(channel)
                error = (None if value is None or ref_value is None
                         else value - ref_value)
                strip.update_channel(channel, error, tol)

        for run_id in list(self.strips):
            if run_id not in self.engines:
                self.strips.pop(run_id).destroy()

        self._refresh_monitor()

        active = sum(1 for e in self.engines.values() if e.is_active)
        self.lbl_runcount.configure(
            text="Nothing running" if not active
            else f"{active} running" + (f" · {len(self.engines) - active} finished"
                                        if len(self.engines) > active else ""))
        if self.strips:
            self.lbl_empty.pack_forget()
        else:
            self.lbl_empty.pack(anchor="w", padx=18, pady=8)

    def _refresh_monitor(self):
        """The focused run, shown large with its stabilisation curve."""
        run_id = self._selected_run_id()
        engine = self.engines.get(run_id)
        if engine is None:
            self.monitor.update_head("Nothing running", "", "idle", "idle")
            self.monitor.update_values("—", "—", "—")
            self.monitor.curve.show([])
            if self._monitor_channels is not None:
                self.monitor.set_channels([], 0.05)
                self._monitor_channels = None
            return

        unit = dict(getattr(engine, "evidence", {}) or {}).get(
            "readout_unit") or "(unit not reported)"
        tol = self._tolerance_for(engine)
        signature = (run_id, tuple(engine.profile.get("dut_channels", [])), tol)
        if signature != self._monitor_channels:
            self.monitor.set_channels(engine.profile.get("dut_channels", []),
                                      tol)
            self._monitor_channels = signature

        kind = {"waiting for stability": "wait", "setting set point": "wait",
                "soaking": "wait", "sampling": "run",
                "finished": "done"}.get(engine.phase, "run")
        if engine.state in ("aborted", "error"):
            kind = "bad"
        elif engine.state == "complete":
            kind = "done"
        self.monitor.update_head(
            engine.profile.get("name", ""),
            f"{engine.heat_source.name} · reference "
            f"{engine.profile.get('reference_channel', '')} · tolerance "
            f"±{tol:g} {unit}",
            engine.phase if engine.is_active else engine.state, kind)

        frame_values, acquired, cycle = self._display_frame(engine)
        ref_value = frame_values.get(engine.profile.get("reference_channel"))
        points = engine.profile.get("setpoints", [])
        self.monitor.update_values(
            "—" if ref_value is None else f"{ref_value:.4f} {unit}",
            "—" if engine.current_setpoint is None
            else f"{engine.current_setpoint:g} {unit}",
            f"{len(engine.results)} of {len(points)} points")

        # keep a rolling history for the curve
        if ref_value is not None and acquired is not None:
            hist = self.history.setdefault(run_id, deque(maxlen=600))
            last_cycle = self.history_cycles.get(run_id)
            if cycle is not None and cycle != last_cycle:
                hist.append((acquired, ref_value))
                self.history_cycles[run_id] = cycle
            window = float(engine.profile.get("stability_window") or 60)
            cutoff = acquired - max(window * 2.5, 30)
            while len(hist) > 2 and hist[0][0] < cutoff:
                hist.popleft()
            self.monitor.curve.show(
                list(hist),
                band=float(engine.profile.get("stability_band") or 0.02),
                setpoint=engine.current_setpoint,
                window=window)

        for channel in engine.profile.get("dut_channels", []):
            value = frame_values.get(channel)
            error = (None if value is None or ref_value is None
                     else value - ref_value)
            self.monitor.update_channel(channel, error, tol)

    @staticmethod
    def _tolerance_for(engine):
        """The pass/fail tolerance at the point being measured."""
        return tolerance_at(engine.profile, max(engine.current_index, 0))

    @staticmethod
    def _flat_text(engine):
        if not engine.is_active:
            return "—"
        if engine.phase == "sampling":
            return "stable"
        window = engine.profile.get("stability_window")
        return f"0 of {window:g} s" if window else "—"

    # -------------------------------------------------------------- results --
    def _refresh_results_choices(self):
        names = []
        for eng in self.engines.values():
            label = f"{eng.profile.get('name','')} ({eng.run_id})"
            if label not in names:
                names.append(label)
        self.cbo_result_run["values"] = names
        if names and self.var_result_run.get() not in names:
            self.var_result_run.set(names[-1])

    def _engine_for_results(self):
        label = self.var_result_run.get()
        for eng in self.engines.values():
            if label in (f"{eng.profile.get('name','')} ({eng.run_id})",
                         eng.profile.get("name", "")):
                return eng
        return None

    def _show_results(self):
        eng = self._engine_for_results()
        for row in self.tbl_results.get_children():
            self.tbl_results.delete(row)
        for row in self.tbl_raw.get_children():
            self.tbl_raw.delete(row)
        if eng is None:
            self.plot.show([])
            self.tbl_raw.configure(columns=())
            self.lbl_result_status.configure(text="NO RUN SELECTED",
                                             style="Invalid.TLabel")
            return
        duts = list(eng.profile.get("dut_channels", []))
        overall = export._overall(eng)
        if overall.startswith("PASS"):
            status_text, status_style = "PASSED", "Pass.TLabel"
        elif overall.startswith("FAIL"):
            status_text, status_style = "FAILED", "Fail.TLabel"
        elif overall.startswith("INCOMPLETE"):
            status_text, status_style = "INCOMPLETE", "Invalid.TLabel"
        elif overall.startswith("NOT VALID"):
            status_text, status_style = "NOT VALID", "Invalid.TLabel"
        else:
            status_text, status_style = "NOT ASSESSED", "Invalid.TLabel"
        self.lbl_result_status.configure(text=status_text, style=status_style)
        series = {c: [] for c in duts}
        for r in eng.results:
            ref = r.reference or {}
            for ch in duts:
                d = r.duts.get(ch, {})
                verdict = d.get("in_tolerance")
                point_invalid = r.verdict == "invalid"
                self.tbl_results.insert(
                    "", "end",
                    tags=(("invalid",) if point_invalid else
                          ("fail",) if verdict is False else ()),
                    values=(f"{r.setpoint:g}",
                            "yes" if r.stable else "NO",
                            _f(ref.get("mean")), _f(ref.get("sd"), 4),
                            ch, _f(d.get("mean")), _f(d.get("sd"), 4),
                            _f(d.get("error")),
                            "" if d.get("tolerance") is None
                            else f"±{d['tolerance']:g}",
                            "NOT VALID" if point_invalid else
                            "" if verdict is None else
                            ("PASS" if verdict else "FAIL"),
                            d.get("n", 0)))
                if d.get("error") is not None and not point_invalid:
                    series[ch].append((r.setpoint, d["error"]))

        raw_columns = ["setpoint", "phase", "sample", "device_time",
                       "timestamp", "cycle", "source", "reference"] + [
                           f"dut_{index}" for index, _ in enumerate(duts)] + [
                           f"dut_time_{index}" for index, _ in enumerate(duts)] + [
                            "source_setpoint", "source_setpoint_unit_raw",
                            "source_setpoint_unit", "source_setpoint_confirmed",
                            "source_unit_raw",
                            "source_verified_unit",
                            "source_unit_query_succeeded",
                            "source_unit_verified"]
        self.tbl_raw.configure(columns=raw_columns)
        raw_titles = ["Set point", "Phase", "Sample",
                      "Device acquisition time", "Host receipt time",
                      "Scan cycle", "Source", "Reference"] + duts + [
                           f"{channel} device acquisition time" for channel in duts] + [
                           "Heat-source set point (device text)",
                            "Set-point reply unit token (device text)",
                            "Set-point reply unit",
                            "Set-point readback confirmed",
                            "Live unit query (device text)",
                           "Live unit query (parsed)",
                           "Live unit query succeeded",
                           "Unit evidence valid"]
        raw_widths = ([90, 85, 65, 210, 230, 90, 190, 120]
                      + [120] * len(duts) + [210] * len(duts)
                      + [220, 190, 120, 150, 190, 120, 150, 130])
        for column, title, width in zip(raw_columns, raw_titles, raw_widths):
            self.tbl_raw.heading(column, text=title)
            self.tbl_raw.column(column, width=width, minwidth=60, anchor="w")
        for result in eng.results:
            for check in getattr(result, "source_checks", ()):
                timestamp = datetime.fromtimestamp(check["t"]).astimezone(
                ).isoformat(timespec="microseconds")
                values = [
                    repr(float(result.setpoint)), "source check",
                    check.get("context", ""), "", timestamp, "",
                    "Heat-source set-point readback", "",
                ] + [""] * (2 * len(duts)) + [
                    check.get("raw", ""),
                    check.get("readback_unit_raw", ""),
                    check.get("readback_unit", ""),
                    "yes" if check.get("confirmed") else "NO",
                    check.get("unit_raw", ""), check.get("unit", ""),
                    "yes" if check.get("unit_query_succeeded") else "NO",
                    "yes" if check.get("unit_verified") else "NO"]
                self.tbl_raw.insert("", "end", values=values)
            for index, sample in enumerate(
                    getattr(result, "stability_samples", ()), 1):
                timestamp = datetime.fromtimestamp(sample["t"]).astimezone(
                ).isoformat(timespec="microseconds")
                values = [
                    f"{result.setpoint:g}", "stability", index,
                    sample.get("device_timestamp", ""), timestamp,
                    sample.get("cycle", ""), sample.get("source", ""),
                    sample.get("ref_raw", ""),
                ] + [""] * (2 * len(duts)) + [
                    "", "", "", "", "", "", "", ""]
                self.tbl_raw.insert("", "end", values=values)
            for index, sample in enumerate(result.samples, 1):
                raw_duts = sample.get("duts_raw", {})
                timestamp = datetime.fromtimestamp(sample["t"]).astimezone(
                ).isoformat(timespec="microseconds")
                values = [
                    f"{result.setpoint:g}", "sampling", index,
                    sample.get("device_timestamp", ""), timestamp,
                    sample.get("cycle", ""), sample.get("source", ""),
                    sample.get("ref_raw", ""),
                ] + [raw_duts.get(channel) or
                     "" for channel in duts] + [
                          sample.get("device_timestamps", {}).get(channel, "")
                          for channel in duts] + [
                          sample.get("source_setpoint_raw", ""),
                           sample.get("source_setpoint_unit_raw", ""),
                           sample.get("source_setpoint_unit", ""),
                           ("yes" if sample.get("source_setpoint_confirmed")
                            else "NO"),
                           sample.get("source_unit_raw", ""),
                          sample.get("source_verified_unit", ""),
                          ("yes" if sample.get("source_unit_query_succeeded")
                           else "NO"),
                          "yes" if sample.get("source_unit_verified") else "NO"]
                self.tbl_raw.insert("", "end", values=values)
        unit = dict(getattr(eng, "evidence", {}) or {}).get(
            "readout_unit") or eng.heat_source.unit
        self.plot.show(
            [{"name": c, "points": pts} for c, pts in series.items() if pts],
            x_label=f"Set point ({unit})",
            y_label="Error (DUT − reference)",
            title=f"{eng.profile.get('name','')} — error against the reference")

    def _export_csv(self):
        eng = self._engine_for_results()
        if eng is None or not eng.results:
            messagebox.showinfo("Export", "No results to export yet.")
            return
        if eng.is_active:
            messagebox.showinfo(
                "Run still active",
                "Finish or stop this calibration before exporting. This keeps "
                "the summary and immutable device-reading files on the same "
                "final result set.")
            return
        folder = filedialog.askdirectory(title="Choose a folder for the CSVs")
        if not folder:
            return
        try:
            paths = export.export_run(eng, self.adt, folder)
        except Exception as e:
            messagebox.showerror("Export failed", str(e))
            return
        self._log("PASS", "Exported: " + ", ".join(os.path.basename(p)
                                                   for p in paths))
        messagebox.showinfo("Exported",
                            "Written:\n\n" + "\n".join(paths))

    # ---------------------------------------------------------------- close --
    def _on_close(self):
        active = [e for e in self.engines.values() if e.is_active]
        if active:
            if not messagebox.askyesno(
                    "Runs still going",
                    f"{len(active)} calibration(s) are still running. Stop "
                    "them and close?"):
                return
            for e in active:
                e.stop()
            for e in active:
                e.join(timeout=5.0)
        for s in self.sources.values():
            try:
                s.disconnect()
            except Exception:
                pass
        try:
            self.adt.disconnect()
        finally:
            self.destroy()


def _f(value, places=3):
    return "" if value is None else f"{value:.{places}f}"


def main():                                               # pragma: no cover
    SuiteApp().mainloop()
