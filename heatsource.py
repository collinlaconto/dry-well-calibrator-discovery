"""Heat source control driven by a stored profile.

Each heat source owns its own serial port, so several of them run without
contending. All writes go through range checking: a set point outside the
profile's range raises rather than being clamped.
"""

import threading
import time

from .formats import (CONTROL_PAIRS, ERROR_QUERY, FAMILIES,
                      NONSENSE_COMMAND, SP_READ_CANDIDATES, STABLE_CANDIDATES,
                      TERMINATORS, UNIT_CANDIDATES, VALUE_CANDIDATES,
                      WRITE_PAIRS, describe_unit_token, first_float,
                      second_field, unit_token_for)
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
        # Some instruments (Additel) require the unit alongside the value on
        # a set-point write, and report it as the second field of the
        # set-point read. Whatever they report is echoed straight back.
        self.unit_token = str(profile.get("unit_token") or "")

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
    def identity_serial(self):
        """Serial number from *IDN? (manufacturer, model, serial, version)."""
        parts = [p.strip() for p in (self.idn or "").split(",")]
        return parts[2] if len(parts) > 2 else ""

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
        if self._cmd("sp_read"):
            try:
                self._capture_unit(self.link.query(self._cmd("sp_read")))
            except Exception:
                pass
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
    def _capture_unit(self, reply):
        """Remember the unit the instrument reports alongside a value."""
        token = second_field(reply)
        if token and token != self.unit_token:
            self.unit_token = token
            self.profile["unit_token"] = token
            self.log("INFO", f"{self.name}: unit token is "
                             f"{describe_unit_token(token)}")
        return reply

    @property
    def effective_unit_token(self):
        """The token to substitute for {unit}: reported, stored, or mapped."""
        return (self.unit_token
                or str(self.profile.get("unit_token") or "")
                or unit_token_for(self.unit))

    def format_setpoint_command(self, template, value):
        """Fill {value} and, when required, {unit} in a write template."""
        out = template.replace("{value}", f"{value:.2f}")
        if "{unit}" in out:
            token = self.effective_unit_token
            if not token:
                raise RuntimeError(
                    f"{self.name}: this set-point command needs a unit, but "
                    "none is known yet. Read the set point once (or run "
                    "Check / discover commands) so the instrument can report "
                    "it.")
            out = out.replace("{unit}", token)
        return out

    def read_setpoint(self):
        cmd = self._cmd("sp_read")
        if not cmd:
            return None
        with self.lock:
            return first_float(self._capture_unit(self.link.query(cmd)))

    def read_temperature(self):
        """The heat source's own block/control sensor (not the reference)."""
        cmd = self._cmd("value")
        if not cmd:
            return None
        with self.lock:
            return first_float(self._capture_unit(self.link.query(cmd)))

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
            if "{unit}" in tmpl and not self.effective_unit_token:
                # Ask the instrument what unit it wants before writing.
                try:
                    self._capture_unit(self.link.query(self._cmd("sp_read")))
                except Exception:
                    pass
            command = self.format_setpoint_command(tmpl, value)
            pw = self._cmd("password")
            if send_password and pw:
                self.link.write(pw)
                time.sleep(0.1)
            self.link.write(command)
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
    # ------------------------------------------------------- discovery ----
    def _error_code(self):
        """Read the error queue. Returns (code, text) or (None, '')."""
        try:
            reply = self.link.query(ERROR_QUERY)
        except Exception:
            return (None, "")
        if not reply:
            return (None, "")
        head = reply.split(",", 1)[0].strip()
        try:
            return (int(head), reply)
        except ValueError:
            return (None, reply)

    def _has_error_queue(self):
        """True if the instrument reports errors for bad commands.

        Additel documents SYSTem:ERRor? as the way to check whether a control
        command was accepted, which makes discovery far more reliable than
        guessing from replies. Confirm it works by sending deliberate
        nonsense and checking that it complains.
        """
        try:
            self.link.write("*CLS")
        except Exception:
            return False
        code, _ = self._error_code()
        if code is None:
            return False
        try:
            self.link.query(NONSENSE_COMMAND)
        except Exception:
            return False
        code, _ = self._error_code()
        return code is not None and code != 0

    def _accepted(self, command, use_errors, expect_reply=True):
        """Send one candidate. Returns (accepted, reply)."""
        try:
            if use_errors:
                self.link.write("*CLS")
            reply = (self.link.query(command) if expect_reply
                     else (self.link.write(command) or ""))
        except Exception:
            return (False, "")
        if use_errors:
            code, _text = self._error_code()
            if code is not None:
                return (code == 0, reply)
        # No usable error queue: a parseable reply is the only evidence.
        return (bool(reply), reply)

    def verify_commands(self, test_delta=0.5, restore=True, log=None):
        """Probe candidate commands live and adopt whatever the instrument
        actually answers to.

        Two dialects are tried: standard SCPI SOURce naming (Fluke wells) and
        Additel's own style, where the root keyword is the quantity itself
        (TEMPerature:TARGet rather than SOURce:SPOint). Where the instrument
        keeps an error queue, each candidate is confirmed with SYSTem:ERRor?
        -- which is the only reliable way to test a command that returns
        nothing.

        Only the set-point write changes anything, by a small delta that is
        then restored. The heat/cool control command is discovered from its
        read-only query form and never actuated.
        """
        say = log or (lambda tag, msg: self.log(tag, msg))
        if not self.is_open:
            raise RuntimeError(f"{self.name} is not connected.")
        report = {"idn": "", "adopted": {}, "failed": [], "verified": False,
                  "error_queue": False, "tried": []}

        with self.lock:
            report["idn"] = self.idn or self.link.query("*IDN?")
            say("INFO", f"{self.name} identifies as: "
                        f"{report['idn'] or '(no reply)'}")

            use_errors = self._has_error_queue()
            report["error_queue"] = use_errors
            say("INFO", "Error queue works - each command will be confirmed "
                        "with SYSTem:ERRor?."
                        if use_errors else
                        "No usable error queue; judging commands by their "
                        "replies alone.")

            fam = FAMILIES.get(self.profile.get("family") or "unknown", {})
            fam_cmds = fam.get("commands") or {}

            def order(key, generic):
                seen, out = set(), []
                for cmd in ([self._cmd(key), fam_cmds.get(key)]
                            + list(generic)):
                    if cmd and cmd not in seen:
                        seen.add(cmd)
                        out.append(cmd)
                return out

            def try_candidates(label, candidates, parser):
                for cmd in candidates:
                    accepted, reply = self._accepted(cmd, use_errors)
                    report["tried"].append((label, cmd, reply))
                    value = parser(reply) if reply else None
                    if accepted and value is not None and value != "":
                        say("PASS", f"{label}: {cmd!r} works (reply {reply!r})")
                        return cmd, value
                    say("INFO", f"{label}: {cmd!r} - no")
                say("FAIL", f"{label}: nothing worked.")
                report["failed"].append(label)
                return None, None

            sp_read, original = try_candidates(
                "Set-point read", order("sp_read", SP_READ_CANDIDATES),
                first_float)
            if sp_read:
                report["adopted"]["sp_read"] = sp_read
                self._capture_unit(self.link.query(sp_read))
                if self.unit_token:
                    report["unit_token"] = self.unit_token
                    say("INFO", f"The set point is reported with a unit "
                                f"({describe_unit_token(self.unit_token)}), "
                                "so the write may need it too.")

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

            # Heat/cool control: probe the query form only, never actuate.
            for query, (on_cmd, off_cmd) in CONTROL_PAIRS.items():
                accepted, reply = self._accepted(query, use_errors)
                report["tried"].append(("Output control", query, reply))
                if accepted and reply:
                    report["adopted"]["enable"] = on_cmd
                    report["adopted"]["disable"] = off_cmd
                    say("PASS", f"Output control: {query!r} answers {reply!r} "
                                f"-> using {on_cmd!r} / {off_cmd!r}")
                    break
            else:
                say("WARN", "No remote heat/cool control command found. "
                            "Switch the output on at the front panel, or the "
                            "instrument will not drive to its set points.")

            for cmd in STABLE_CANDIDATES:
                accepted, reply = self._accepted(cmd, use_errors)
                if accepted and reply:
                    say("INFO", f"Instrument also reports its own stability "
                                f"via {cmd!r} ({reply!r}). The suite still "
                                "judges stability from the reference probe.")
                    break

            # Set-point write: only meaningful if we can read it back.
            if sp_read and original is not None:
                write = (self._cmd("sp_write") or fam_cmds.get("sp_write")
                         or WRITE_PAIRS.get(sp_read))
                if not write:
                    say("FAIL", f"Set-point write: no known pairing for "
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
                        # Some instruments want the unit alongside the value.
                        # Try both shapes, preferring the one the set-point
                        # read implies.
                        plain = write.replace(",{unit}", "")
                        with_unit = (plain if "{unit}" in plain
                                     else plain + ",{unit}")
                        variants = ([with_unit, plain] if self.unit_token
                                    else [plain, with_unit])
                        good, chosen, rb = False, None, None
                        for variant in variants:
                            try:
                                command = self.format_setpoint_command(
                                    variant, target)
                            except RuntimeError:
                                continue
                            self._accepted(command, use_errors,
                                           expect_reply=False)
                            time.sleep(0.4)
                            rb = first_float(
                                self._capture_unit(self.link.query(sp_read)))
                            if rb is not None and abs(rb - target) <= 0.05:
                                good, chosen = True, variant
                                break
                            say("INFO", f"Set-point write: {variant!r} did "
                                        f"not take (read back {rb}).")
                        write = chosen or write
                        if good:
                            extra = ("  It needs the unit, supplied "
                                     "automatically from the instrument's own "
                                     "reply." if "{unit}" in write else "")
                            say("PASS", f"Set-point write: {write!r} verified "
                                        f"(wrote {target:.2f}, read {rb})."
                                        + extra)
                            report["adopted"]["sp_write"] = write
                            report["verified"] = True
                        elif rb is None:
                            # No reply at all: distinguish a dropped link
                            # from a rejected command before blaming a lock.
                            alive = bool(self.link.query("*IDN?"))
                            if alive:
                                say("FAIL", f"Set-point write: wrote "
                                            f"{target:.2f} but the set point "
                                            "could not be read back. The "
                                            "command may have been rejected.")
                            else:
                                say("FAIL", "The instrument stopped "
                                            "responding during the set-point "
                                            "test - the connection dropped. "
                                            "Reconnect and try again; nothing "
                                            "here says the command is wrong.")
                                report["link_lost"] = True
                            report["failed"].append("Set-point write")
                            report["adopted"]["sp_write"] = write
                        else:
                            say("FAIL", f"Set-point write: wrote "
                                        f"{target:.2f} but read back {rb}. "
                                        "The set point may be locked on the "
                                        "instrument.")
                            report["failed"].append("Set-point write")
                            report["adopted"]["sp_write"] = write
                        if restore:
                            try:
                                self.link.write(
                                    self.format_setpoint_command(write,
                                                                 original))
                                time.sleep(0.3)
                                say("INFO", f"Set point restored to "
                                            f"{original:g}.")
                            except RuntimeError as exc:
                                say("WARN", f"Could not restore the set "
                                            f"point: {exc}")

        self.profile.update(report["adopted"])
        self.profile["verified"] = report["verified"]
        if not report["failed"]:
            say("PASS", f"{self.name}: all commands verified on the "
                        "instrument.")
        return report
