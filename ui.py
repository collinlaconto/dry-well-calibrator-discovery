"""Tkinter interface for the calibration suite."""

import json
import os
import queue
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

from . import export
from .adt286 import Adt286
from .engine import (ChannelRegistry, RunEngine, default_profile,
                     validate_profile, STATE_RUNNING)
from .formats import KNOWN_MODELS, profile_for_model
from .heatsource import HeatSource
from .transport import (CANDIDATE_TCP_PORTS, SERIAL_OK, SERIAL_ERROR,
                        available_ports, describe_target, find_tcp_port,
                        normalize_target, target_is_set)

APP_TITLE = "Temperature Calibration Suite — ADT286 + heat sources"
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
        self.var_tcp = tk.StringVar(value="5025")
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
        ttk.Button(self.row_serial, text="Refresh",
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
        ttk.Button(self.row_tcp, text="Find port",
                   command=self.find_port).pack(side="left", padx=8)
        self.lbl_hint = ttk.Label(self, text="", foreground="#555",
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
                port = int(str(self.var_tcp.get()).strip() or 5025)
            except ValueError:
                port = 5025
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
            self.var_tcp.set(str(t.get("tcp_port", 5025)))
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
        self.title(APP_TITLE)
        self.geometry("1180x820")
        self.minsize(1000, 700)

        self.adt = Adt286(logger=self._instrument_log)
        self.registry = ChannelRegistry()
        self.sources = {}            # name -> HeatSource
        self.run_profiles = self._load(RUN_LIB, [])
        self.source_profiles = self._load(SOURCE_LIB, [])
        self.engines = {}            # run_id -> RunEngine
        self.events = queue.Queue()
        self.log_queue = queue.Queue()
        self._run_seq = 0

        self._build_ui()
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
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            messagebox.showerror("Saving failed", f"Could not write {path}:\n{e}")

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
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self._build_instruments_tab()
        self._build_profiles_tab()
        self._build_runs_tab()
        self._build_results_tab()

        bar = ttk.LabelFrame(self, text="Activity")
        bar.pack(fill="both", padx=8, pady=(0, 8))
        self.log_main = scrolledtext.ScrolledText(bar, height=7, wrap="word")
        self.log_main.pack(fill="both", expand=True, padx=6, pady=6)
        for tag, colour in (("TX", "#0b5cad"), ("RX", "#20603d"),
                            ("PASS", "#1a7f37"), ("FAIL", "#b00020"),
                            ("WARN", "#9a6700"), ("INFO", "#444")):
            self.log_main.tag_config(tag, foreground=colour)
        self.log_main.configure(state="disabled")

    # --- tab 1 -------------------------------------------------------------
    def _build_instruments_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 1 · Instruments ")
        pad = {"padx": 6, "pady": 4}

        adt = ttk.LabelFrame(t, text="Additel ADT286 (USB)")
        adt.pack(fill="x", padx=8, pady=8)
        self.pick_adt = ConnectionPicker(adt, on_log=self._instrument_log)
        self.pick_adt.grid(row=0, column=0, columnspan=6, sticky="w", **pad)
        btns = ttk.Frame(adt)
        btns.grid(row=1, column=0, columnspan=6, sticky="w", padx=6)
        ttk.Button(btns, text="Connect",
                   command=self._connect_adt).pack(side="left", padx=4)
        ttk.Button(btns, text="Disconnect",
                   command=self._disconnect_adt).pack(side="left", padx=4)
        self.lbl_adt = ttk.Label(btns, text="Not connected",
                                 foreground="#b00020")
        self.lbl_adt.pack(side="left", padx=10)
        ttk.Label(adt, foreground="#555", wraplength=980, justify="left",
                  text=("Channels are read as the 286 has them configured. "
                        "The suite never changes the 286's channel setup or "
                        "units, because those are global and other runs "
                        "would be affected — set sensor types on the "
                        "instrument before starting.")).grid(
            row=2, column=0, columnspan=6, sticky="w", padx=8)
        self.lst_channels = tk.Listbox(adt, height=7, width=52)
        self.lst_channels.grid(row=3, column=0, columnspan=3, sticky="w",
                               padx=8, pady=6)
        self.lbl_channel_use = ttk.Label(adt, text="", justify="left",
                                         foreground="#444")
        self.lbl_channel_use.grid(row=3, column=3, columnspan=3, sticky="nw",
                                  padx=8, pady=6)

        hs = ttk.LabelFrame(t, text="Heat sources (each on its own "
                                    "connection)")
        hs.pack(fill="both", expand=True, padx=8, pady=8)
        row = ttk.Frame(hs)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Add").pack(side="left")
        self.var_new_source = tk.StringVar()
        self.cbo_new_source = ttk.Combobox(row, textvariable=self.var_new_source,
                                           width=46, state="readonly")
        self.cbo_new_source.pack(side="left", padx=6)
        ttk.Button(row, text="Connect heat source",
                   command=self._connect_source).pack(side="left", padx=6)
        ttk.Button(row, text="Disconnect selected",
                   command=self._disconnect_source).pack(side="left")
        ttk.Button(row, text="Check / discover commands",
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
        pad = {"padx": 6, "pady": 3}

        left = ttk.LabelFrame(t, text="Calibration profiles")
        left.pack(side="left", fill="y", padx=8, pady=8)
        self.lst_profiles = tk.Listbox(left, width=30, height=24,
                                       exportselection=False)
        self.lst_profiles.pack(fill="y", expand=True, padx=6, pady=6)
        self.lst_profiles.bind("<<ListboxSelect>>", self._select_profile)
        for txt, cmd in (("New", self._new_profile),
                         ("Save", self._save_profile),
                         ("Duplicate", self._duplicate_profile),
                         ("Delete", self._delete_profile)):
            ttk.Button(left, text=txt, command=cmd).pack(fill="x", padx=6,
                                                         pady=2)

        form = ttk.Frame(t)
        form.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        basic = ttk.LabelFrame(form, text="What to calibrate")
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
        ttk.Label(basic, foreground="#555",
                  text="Ctrl-click to pick several. A channel can belong to "
                       "only one running calibration at a time.").grid(
            row=4, column=1, sticky="w", padx=6)

        sp = ttk.LabelFrame(form, text="Set points")
        sp.pack(fill="x", pady=4)
        self.var_p_setpoints = tk.StringVar()
        ttk.Label(sp, text="Set points (°, comma separated)").grid(
            row=0, column=0, sticky="e", **pad)
        ttk.Entry(sp, textvariable=self.var_p_setpoints, width=52).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Label(sp, foreground="#555",
                  text="They run in the order given — e.g. 0, 50, 100, 50, 0 "
                       "for an up-and-down sequence.").grid(
            row=1, column=1, sticky="w", padx=6)

        stab = ttk.LabelFrame(form, text="Stability and sampling")
        stab.pack(fill="x", pady=4)
        self.var_p_band = tk.StringVar(value="0.02")
        self.var_p_window = tk.StringVar(value="60")
        self.var_p_maxwait = tk.StringVar(value="2400")
        self.var_p_count = tk.StringVar(value="10")
        self.var_p_interval = tk.StringVar(value="5")
        self.var_p_soak = tk.StringVar(value="0")
        self.var_p_near = tk.BooleanVar(value=False)
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
        ttk.Checkbutton(stab, text="Also require the reference to be near the "
                                   "set point",
                        variable=self.var_p_near).grid(row=3, column=0,
                                                       columnspan=2,
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

        act = ttk.Frame(form)
        act.pack(fill="x", pady=6)
        ttk.Button(act, text="Check this profile",
                   command=self._validate_profile).pack(side="left")
        ttk.Button(act, text="Save and start run",
                   command=self._save_and_start).pack(side="left", padx=8)

    # --- tab 3 -------------------------------------------------------------
    def _build_runs_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 3 · Runs ")
        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Start").pack(side="left")
        self.var_run_choice = tk.StringVar()
        self.cbo_run_choice = ttk.Combobox(bar, textvariable=self.var_run_choice,
                                           width=34, state="readonly")
        self.cbo_run_choice.pack(side="left", padx=6)
        ttk.Button(bar, text="Start run",
                   command=self._start_selected).pack(side="left")
        ttk.Button(bar, text="Stop selected run",
                   command=self._stop_selected).pack(side="left", padx=8)
        ttk.Button(bar, text="STOP EVERYTHING",
                   command=self._stop_all).pack(side="right")

        cols = ("name", "source", "state", "phase", "setpoint", "progress",
                "reference")
        self.tbl_runs = ttk.Treeview(t, columns=cols, show="headings",
                                     height=10)
        for c, w, txt in (("name", 190, "Run"), ("source", 170, "Heat source"),
                          ("state", 90, "State"), ("phase", 160, "Phase"),
                          ("setpoint", 90, "Set point"),
                          ("progress", 110, "Points done"),
                          ("reference", 130, "Reference now")):
            self.tbl_runs.heading(c, text=txt)
            self.tbl_runs.column(c, width=w, anchor="w")
        self.tbl_runs.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(t, foreground="#555", wraplength=1000, justify="left",
                  text=("Runs share the one 286: their channels are scanned "
                        "together and the readings fanned out. Starting or "
                        "finishing a run reconfigures that shared scan, which "
                        "pauses data for a second — harmless, but it is why "
                        "a run's channels are locked to it while it is "
                        "active.")).pack(fill="x", padx=10, pady=(0, 8))

    # --- tab 4 -------------------------------------------------------------
    def _build_results_tab(self):
        t = ttk.Frame(self.nb)
        self.nb.add(t, text=" 4 · Results ")
        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Run").pack(side="left")
        self.var_result_run = tk.StringVar()
        self.cbo_result_run = ttk.Combobox(bar, textvariable=self.var_result_run,
                                           width=36, state="readonly")
        self.cbo_result_run.pack(side="left", padx=6)
        self.cbo_result_run.bind("<<ComboboxSelected>>",
                                 lambda e: self._show_results())
        ttk.Button(bar, text="Refresh",
                   command=self._show_results).pack(side="left")
        ttk.Button(bar, text="Export CSV",
                   command=self._export_csv).pack(side="left", padx=8)

        cols = ("setpoint", "stable", "ref", "refsd", "channel", "mean", "sd",
                "error", "n")
        self.tbl_results = ttk.Treeview(t, columns=cols, show="headings",
                                        height=10)
        for c, w, txt in (("setpoint", 90, "Set point"),
                          ("stable", 70, "Stable"),
                          ("ref", 120, "Reference"), ("refsd", 90, "Ref SD"),
                          ("channel", 110, "DUT channel"),
                          ("mean", 120, "DUT mean"), ("sd", 90, "DUT SD"),
                          ("error", 110, "Error"), ("n", 50, "n")):
            self.tbl_results.heading(c, text=txt)
            self.tbl_results.column(c, width=w, anchor="w")
        self.tbl_results.pack(fill="both", expand=True, padx=8, pady=6)
        self.plot = PlotCanvas(t, height=300)
        self.plot.pack(fill="both", expand=True, padx=8, pady=(0, 8))

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
        self.cbo_p_ref["values"] = self.adt.channels
        self.lst_p_duts.delete(0, "end")
        for c in self.adt.channels:
            self.lst_p_duts.insert("end", c)

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
        name = profile.get("name", "heat source")
        if name in self.sources:
            messagebox.showerror("Already connected",
                                 f"{name} is already connected.")
            return
        source = HeatSource(profile, logger=self._instrument_log)
        try:
            source.connect(target)
        except Exception as e:
            messagebox.showerror("Heat source",
                                 f"Could not connect {name} on {where}:\n{e}")
            return
        self.sources[name] = source
        self._refresh_source_table()
        self.cbo_p_source["values"] = list(self.sources)
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
        for key in ("sp_write", "sp_read", "value", "unit"):
            if key in adopted:
                lines.append(f"  {key:9s} {adopted[key]}")
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
        p["heat_source"] = self.var_p_source.get()
        p["reference_channel"] = self.var_p_ref.get()
        p["dut_channels"] = [self.lst_p_duts.get(i)
                             for i in self.lst_p_duts.curselection()]
        p["setpoints"] = self._parse_setpoints(self.var_p_setpoints.get())
        for key, var, cast in (
                ("stability_band", self.var_p_band, float),
                ("stability_window", self.var_p_window, float),
                ("max_wait", self.var_p_maxwait, float),
                ("sample_count", self.var_p_count, int),
                ("sample_interval", self.var_p_interval, float),
                ("soak_seconds", self.var_p_soak, float),
                ("setpoint_tolerance", self.var_p_sptol, float)):
            try:
                p[key] = cast(str(var.get()).strip().replace(",", "."))
            except ValueError:
                pass
        p["require_near_setpoint"] = bool(self.var_p_near.get())
        p["enable_output"] = bool(self.var_p_enable.get())
        p["disable_at_end"] = bool(self.var_p_disable.get())
        p["send_password"] = bool(self.var_p_pw.get())
        p["on_timeout"] = self.var_p_timeout.get()
        return p

    @staticmethod
    def _parse_setpoints(text):
        out = []
        for chunk in (text or "").replace(";", ",").split(","):
            chunk = chunk.strip().replace(" ", "")
            if not chunk:
                continue
            try:
                out.append(float(chunk))
            except ValueError:
                pass
        return out

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
        self.var_p_near.set(bool(p.get("require_near_setpoint")))
        self.var_p_enable.set(bool(p.get("enable_output", True)))
        self.var_p_disable.set(bool(p.get("disable_at_end", True)))
        self.var_p_pw.set(bool(p.get("send_password")))
        self.var_p_timeout.set(p.get("on_timeout", "record"))
        wanted = set(p.get("dut_channels", []))
        self.lst_p_duts.selection_clear(0, "end")
        for i in range(self.lst_p_duts.size()):
            if self.lst_p_duts.get(i) in wanted:
                self.lst_p_duts.selection_set(i)

    def _select_profile(self, _e=None):
        sel = self.lst_profiles.curselection()
        if sel:
            self._profile_to_form(self.run_profiles[sel[0]])

    def _new_profile(self):
        self._profile_to_form(default_profile())
        self.var_p_name.set("")

    def _save_profile(self):
        p = self._form_to_profile()
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
        problems = validate_profile(p, hs, self.adt.channels or None)
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
        sel = self.tbl_runs.selection()
        return sel[0] if sel else None

    def _stop_selected(self):
        run_id = self._selected_run_id()
        if not run_id or run_id not in self.engines:
            messagebox.showinfo("Stop run",
                                "Select a run in the table first.")
            return
        self.engines[run_id].stop()
        self._log("WARN", f"Stopping '{self.engines[run_id].profile.get('name')}'…")

    def _stop_all(self):
        active = [e for e in self.engines.values() if e.is_active]
        if not active:
            return
        for e in active:
            e.stop()
        self._log("WARN", f"Stopping {len(active)} run(s); outputs will be "
                          "switched off where the profile allows.")

    def _refresh_run_table(self):
        existing = set(self.tbl_runs.get_children())
        for run_id, eng in self.engines.items():
            ref = ("" if eng.last_reference is None
                   else f"{eng.last_reference:.3f}")
            values = (eng.profile.get("name", ""), eng.heat_source.name,
                      eng.state, eng.phase,
                      "" if eng.current_setpoint is None
                      else f"{eng.current_setpoint:g}",
                      f"{len(eng.results)}/{len(eng.profile.get('setpoints', []))}",
                      ref)
            if run_id in existing:
                self.tbl_runs.item(run_id, values=values)
            else:
                self.tbl_runs.insert("", "end", iid=run_id, values=values)

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
        if eng is None:
            self.plot.show([])
            return
        duts = list(eng.profile.get("dut_channels", []))
        series = {c: [] for c in duts}
        for r in eng.results:
            ref = r.reference or {}
            for ch in duts:
                d = r.duts.get(ch, {})
                self.tbl_results.insert(
                    "", "end",
                    values=(f"{r.setpoint:g}",
                            "yes" if r.stable else "NO",
                            _f(ref.get("mean")), _f(ref.get("sd"), 4),
                            ch, _f(d.get("mean")), _f(d.get("sd"), 4),
                            _f(d.get("error")), d.get("n", 0)))
                if d.get("error") is not None:
                    series[ch].append((r.setpoint, d["error"]))
        self.plot.show(
            [{"name": c, "points": pts} for c, pts in series.items() if pts],
            x_label=f"Set point ({eng.heat_source.unit})",
            y_label="Error (DUT − reference)",
            title=f"{eng.profile.get('name','')} — error against the reference")

    def _export_csv(self):
        eng = self._engine_for_results()
        if eng is None or not eng.results:
            messagebox.showinfo("Export", "No results to export yet.")
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
