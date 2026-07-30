"""Heat source control driven by a stored profile.

Each heat source owns its own serial port, so several of them run without
contending. All writes go through range checking: a set point outside the
profile's range raises rather than being clamped.
"""

import threading
import time

from .formats import (FAMILIES, SP_READ_CANDIDATES, TERMINATORS,
                      UNIT_CANDIDATES, VALUE_CANDIDATES,
                      WRITE_PAIRS, first_float)
from .transport import (describe_target, make_link, normalize_target,
                        target_is_set)


class RangeError(ValueError):
    """Raised when a set point falls outside the profile's declared range."""


class HeatSource:
    def __init__(self, profile, logger=None):
        self.profile = dict(profile)
        self.log = logger or (lambda tag, msg: None)
        self.lock = threading.RLock()
        self.terminator = TERMINATORS.get(
            profile.get("terminator_name", "CRLF"), "\r\n")
        self.link = None
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
        return self.link is not None and self.link.is_open

    @property
    def target(self):
        return normalize_target(self.profile.get("target")
                                or self.profile.get("port"),
                                self.profile.get("baud") or "9600")

    @property
    def connection(self):
        """One-line description of how this instrument is reached."""
        return describe_target(self.target)

    def connect(self, target=None):
        """Connect over serial/Bluetooth SPP or the network."""
        t = normalize_target(target if target is not None else self.target,
                             self.profile.get("baud") or "9600")
        if not target_is_set(t):
            raise RuntimeError(f"{self.name}: no port or address given.")
        with self.lock:
            self.link = make_link(t, terminator=self.terminator,
                                  reply_timeout=1.5)
            self.link.open(t)
            try:
                self.idn = self.link.query("*IDN?")
            except Exception:
                self.idn = ""
        self.profile["target"] = t
        if t["kind"] in ("serial", "bluetooth"):
            self.profile["port"] = t.get("port", "")
            self.profile["baud"] = str(t.get("baud", "9600"))
        self.log("PASS", f"{self.name} connected on {describe_target(t)}"
                         + (f" - {self.idn}" if self.idn else ""))
        return self.idn

    def disconnect(self):
        with self.lock:
            if self.link is not None:
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

    # ------------------------------------------------------ verification ----
    def verify_commands(self, test_delta=0.5, restore=True, log=None):
        """Probe candidate commands live and adopt whatever the instrument
        actually answers.

        This is how a heat source whose syntax isn't documented (the Additel
        878s, for instance) gets a working profile: nothing is assumed, each
        command has to prove itself on the instrument. Only the set-point write
        makes a change, by a small delta that is then restored, and the output
        enable is never sent.

        Returns a report dict; adopted commands are written into the profile.
        """
        say = log or (lambda tag, msg: self.log(tag, msg))
        if not self.is_open:
            raise RuntimeError(f"{self.name} is not connected.")
        report = {"idn": "", "adopted": {}, "failed": [], "verified": False}

        with self.lock:
            report["idn"] = self.idn or self.link.query("*IDN?")
            say("INFO", f"{self.name} identifies as: "
                        f"{report['idn'] or '(no reply)'}")

            def try_candidates(label, candidates, parser):
                for cmd in candidates:
                    if not cmd:
                        continue
                    reply = self.link.query(cmd)
                    value = parser(reply) if reply else None
                    if value is not None and value != "":
                        say("PASS", f"{label}: {cmd!r} works (reply {reply!r})")
                        return cmd, value
                    say("INFO", f"{label}: {cmd!r} - no usable reply")
                say("FAIL", f"{label}: nothing worked. Enter it by hand, or "
                            "check the instrument's command document.")
                report["failed"].append(label)
                return None, None

            # order: whatever the profile already has, then family, then generic
            fam = FAMILIES.get(self.profile.get("family") or "unknown", {})
            fam_cmds = fam.get("commands") or {}

            def order(key, generic):
                seen, out = set(), []
                for cmd in ([self._cmd(key), fam_cmds.get(key)] + list(generic)):
                    if cmd and cmd not in seen:
                        seen.add(cmd)
                        out.append(cmd)
                return out

            sp_read, original = try_candidates(
                "Set-point read", order("sp_read", SP_READ_CANDIDATES),
                first_float)
            if sp_read:
                report["adopted"]["sp_read"] = sp_read

            value_cmd, _ = try_candidates(
                "Value (block temperature)", order("value", VALUE_CANDIDATES),
                first_float)
            if value_cmd:
                report["adopted"]["value"] = value_cmd

            unit_cmd, _ = try_candidates(
                "Unit read", order("unit", UNIT_CANDIDATES),
                lambda r: (r or "").strip())
            if unit_cmd:
                report["adopted"]["unit"] = unit_cmd

            # set-point write: only meaningful if we can read it back
            if sp_read and original is not None:
                write = self._cmd("sp_write") or fam_cmds.get("sp_write") \
                    or WRITE_PAIRS.get(sp_read)
                if not write:
                    say("FAIL", "Set-point write: no known pairing for "
                                f"{sp_read!r}; enter it by hand.")
                    report["failed"].append("Set-point write")
                else:
                    lo, hi = self.range
                    target = original + test_delta
                    if lo is not None and not (lo <= target <= hi):
                        target = original - test_delta
                    if lo is not None and not (lo <= target <= hi):
                        target = original
                    if abs(target - original) < 1e-9:
                        say("WARN", "Range too tight to test a set-point "
                                    "change; write command not verified.")
                        report["adopted"]["sp_write"] = write
                    else:
                        self.link.write(write.replace("{value}",
                                                      f"{target:.2f}"))
                        time.sleep(0.4)
                        rb = first_float(self.link.query(sp_read))
                        if rb is not None and abs(rb - target) <= 0.05:
                            say("PASS", f"Set-point write: {write!r} verified "
                                        f"(wrote {target:.2f}, read {rb}).")
                            report["adopted"]["sp_write"] = write
                            report["verified"] = True
                        else:
                            say("FAIL", f"Set-point write: wrote {target:.2f} "
                                        f"but read back {rb}. The set point "
                                        "may be locked on the instrument.")
                            report["failed"].append("Set-point write")
                            report["adopted"]["sp_write"] = write
                        if restore:
                            self.link.write(write.replace("{value}",
                                                          f"{original:.2f}"))
                            time.sleep(0.3)
                            say("INFO", f"Set point restored to {original:g}.")

        self.profile.update(report["adopted"])
        self.profile["verified"] = report["verified"]
        if not report["failed"]:
            say("PASS", f"{self.name}: all commands verified on the "
                        "instrument.")
        return report
