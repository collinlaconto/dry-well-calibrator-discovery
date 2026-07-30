"""Heat source control driven by a stored profile.

Each heat source owns its own serial port, so several of them run without
contending. All writes go through range checking: a set point outside the
profile's range raises rather than being clamped.
"""

import threading
import time

from .formats import FAMILIES, TERMINATORS, first_float
from .transport import SerialLink


class RangeError(ValueError):
    """Raised when a set point falls outside the profile's declared range."""


class HeatSource:
    def __init__(self, profile, logger=None):
        self.profile = dict(profile)
        self.log = logger or (lambda tag, msg: None)
        self.lock = threading.RLock()
        self.link = SerialLink(
            terminator=TERMINATORS.get(profile.get("terminator_name", "CRLF"),
                                       "\r\n"),
            reply_timeout=1.5)
        self.idn = ""

    # ------------------------------------------------------------ helpers --
    @property
    def name(self):
        return self.profile.get("name") or self.profile.get("model", "heat source")

    @property
    def unit(self):
        return self.profile.get("range_unit", "°C")

    @property
    def range(self):
        try:
            return (float(self.profile["range_min"]),
                    float(self.profile["range_max"]))
        except (KeyError, TypeError, ValueError):
            return (None, None)

    def _cmd(self, key):
        return (self.profile.get(key) or "").strip()

    # -------------------------------------------------------- connection ---
    @property
    def is_open(self):
        return self.link.is_open

    def connect(self, port, baud=None):
        baud = baud or self.profile.get("baud") or "9600"
        with self.lock:
            self.link.open(port, baud,
                           TERMINATORS.get(
                               self.profile.get("terminator_name", "CRLF"),
                               "\r\n"))
            try:
                self.idn = self.link.query("*IDN?")
            except Exception:
                self.idn = ""
        self.profile["port"] = port
        self.profile["baud"] = str(baud)
        self.log("PASS", f"{self.name} connected on {port} @ {baud}")
        return self.idn

    def disconnect(self):
        with self.lock:
            self.link.close()
        self.log("INFO", f"{self.name} disconnected.")

    # ------------------------------------------------------------ control --
    def read_setpoint(self):
        cmd = self._cmd("sp_read")
        if not cmd:
            return None
        with self.lock:
            return first_float(self.link.query(cmd))

    def read_temperature(self):
        """The heat source's own block/control sensor (not the reference)."""
        cmd = self._cmd("value")
        if not cmd:
            return None
        with self.lock:
            return first_float(self.link.query(cmd))

    def set_setpoint(self, value, send_password=False):
        tmpl = self._cmd("sp_write")
        if not tmpl or "{value}" not in tmpl:
            raise RuntimeError(
                f"{self.name}: no usable set-point writing command in the "
                "profile (needs a {value} placeholder).")
        lo, hi = self.range
        if lo is not None and hi is not None and not (lo <= value <= hi):
            raise RangeError(
                f"{value:g} {self.unit} is outside {self.name}'s range "
                f"({lo:g} to {hi:g} {self.unit}). Nothing was sent.")
        with self.lock:
            pw = self._cmd("password")
            if send_password and pw:
                self.link.write(pw)
                time.sleep(0.1)
            self.link.write(tmpl.replace("{value}", f"{value:.2f}"))
        self.log("INFO", f"{self.name}: set point -> {value:g} {self.unit}")
        return True

    def confirm_setpoint(self, value, tolerance=0.05, settle=0.4):
        """Read the set point back. Returns (ok, readback)."""
        if not self._cmd("sp_read"):
            return (None, None)
        time.sleep(settle)
        rb = self.read_setpoint()
        if rb is None:
            return (False, None)
        return (abs(rb - value) <= tolerance, rb)

    def enable_output(self):
        cmd = self._cmd("enable")
        if not cmd:
            return False
        with self.lock:
            self.link.write(cmd)
        self.log("INFO", f"{self.name}: output enabled")
        return True

    def disable_output(self):
        cmd = self._cmd("disable")
        if not cmd:
            return False
        with self.lock:
            self.link.write(cmd)
        self.log("INFO", f"{self.name}: output disabled")
        return True

    # ----------------------------------------------------------- checking --
    def check_setpoints(self, setpoints):
        """Return a list of set points that fall outside the profile range."""
        lo, hi = self.range
        if lo is None or hi is None:
            return []
        return [sp for sp in setpoints if not (lo <= sp <= hi)]

    def family_checklist(self):
        fam = FAMILIES.get(self.profile.get("family") or "unknown")
        return list(fam.get("checklist", []))
