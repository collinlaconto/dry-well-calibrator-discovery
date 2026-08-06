"""Heat source control driven by a stored profile.

Each heat source owns its own serial port, so several of them run without
contending. All writes go through range checking: a set point outside the
profile's range raises rather than being clamped.
"""

import threading
import time

from .formats import (CONTROL_PAIRS, CONTROL_STATUS_CANDIDATES, ERROR_QUERY,
                      FAMILIES, NONSENSE_COMMAND, SP_READ_CANDIDATES,
                      STABLE_CANDIDATES, TERMINATORS, UNIT_CANDIDATES,
                      VALUE_CANDIDATES, WRITE_PAIRS, describe_unit_token,
                      first_float, plausible_unit_token, second_field,
                      unit_token_for)
from .formats import (unit_name_for_token, unit_name_from_reply,
                      unit_token_from_reply)
from .transport import (describe_target, make_link, normalize_target,
                        target_is_set)


class RangeError(ValueError):
    """Raised when a set point falls outside the profile's declared range."""


def error_code_from_reply(reply):
    """Parse an SCPI error reply into ``(code, exact_reply)``."""
    exact = str(reply or "")
    if not exact:
        return (None, "")
    head = exact.split(",", 1)[0].strip()
    try:
        return (int(head), exact)
    except ValueError:
        return (None, exact)


def _is_error_queue_query(command):
    """True for an explicit SYSTem:ERRor[:NEXT]? queue inspection."""
    head = str(command or "").strip().upper().split(None, 1)[0]
    return (head.endswith("?") and head.startswith("SYST")
            and ":ERR" in head)


def is_safe_read_command(command):
    """Whether a configured read is non-chained and cannot be a SCPI write."""
    exact = str(command or "").strip()
    if not exact or any(separator in exact for separator in (";", "\r", "\n")):
        return False
    # Hart classic predates SCPI's question-mark convention.
    if exact.lower() in ("s", "t", "u"):
        return True
    head = exact.split(None, 1)[0]
    # Error queries consume diagnostic state even though they contain '?'.
    return head.endswith("?") and not _is_error_queue_query(exact)


def checked_exchange(link, command, expect_reply=True, check_error=False):
    """Run one command and, when requested, attribute only its own error.

    Additel's error query removes the oldest queued item.  Clear stale items
    immediately before the command, capture its first error afterwards, then
    clear any additional residue.  An explicit error-queue query is left
    untouched so the Terminal can still inspect the queue deliberately.
    Returns ``(reply, error_reply)``; ``error_reply`` is ``None`` when no
    automatic queue check was requested.
    """
    inspect_error = bool(check_error) and not _is_error_queue_query(command)
    if not inspect_error:
        reply = (link.query(command) if expect_reply
                 else (link.write(command) or ""))
        return (reply, None)

    link.write("*CLS")
    try:
        try:
            reply = (link.query(command) if expect_reply
                     else (link.write(command) or ""))
        except TimeoutError:
            # Invalid query headers normally have no data reply.  If the
            # device did record an error, report that useful result instead
            # of exposing a generic read timeout.
            error_reply = link.query(ERROR_QUERY)
            code, _ = error_code_from_reply(error_reply)
            if code not in (None, 0):
                return ("", error_reply)
            raise
        error_reply = link.query(ERROR_QUERY)
        return (reply, error_reply)
    finally:
        try:
            link.write("*CLS")
        except Exception:
            pass


class HeatSource:
    def __init__(self, profile, logger=None):
        self.profile = dict(profile)
        family_name = self.profile.get("family") or "unknown"
        family = FAMILIES.get(family_name, {})
        # Known instrument families use their authoritative read forms. A
        # legacy/manual override such as BOGUS? may look like a query but can
        # still add -110 to the device queue before discovery repairs it.
        if family_name not in ("generic_scpi", "unknown"):
            for key in ("sp_read", "value", "unit"):
                command = (family.get("commands") or {}).get(key)
                if command:
                    self.profile[key] = command
        # Migrate the unsafe Additel defaults written by older suite builds.
        # The ADT878 does not implement OUTP:STAT, and its documented control
        # transition requires a target and unit rather than a context-free
        # enable/disable pair.
        if self.profile.get("family") == "additel_well":
            enable = " ".join(str(self.profile.get("enable") or "").split())
            disable = " ".join(str(self.profile.get("disable") or "").split())
            if enable.upper() == "OUTP:STAT 1":
                self.profile["enable"] = ""
            if disable.upper() == "OUTP:STAT 0":
                self.profile["disable"] = ""
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
        self.reported_unit = unit_name_for_token(self.unit_token)
        self.last_setpoint_command = ""
        self.last_setpoint_readback_raw = ""
        self.last_setpoint_readback_unit_raw = ""
        self.last_setpoint_readback_unit = ""
        self.last_unit_reply = ""
        self.reported_unit = ""

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

    def _read_cmd(self, key):
        """Return a validated stored read command, or fail before transmission."""
        command = self._cmd(key)
        if command and not is_safe_read_command(command):
            raise RuntimeError(
                f"{self.name}: configured {key!r} command {command!r} is not "
                "a safe read query. Nothing was sent.")
        return command

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
        if self.is_open:
            raise RuntimeError(f"{self.name} is already connected.")
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
        # A stored profile token is useful for command formatting, but it is
        # not current device evidence.  Require this connection to prove its
        # live unit again.
        self.reported_unit = ""
        self.last_unit_reply = ""
        try:
            sp_read = self._read_cmd("sp_read")
            if sp_read:
                self._capture_unit(self.link.query(sp_read))
        except Exception as exc:
            self.log("WARN", f"{self.name}: stored set-point read was not "
                             f"used during connection: {exc}")
        try:
            if self._read_cmd("unit"):
                self.read_unit()
        except Exception as exc:
            self.log("WARN", f"{self.name}: stored unit read was not used "
                             f"during connection: {exc}")
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
        if token and not plausible_unit_token(token):
            self.reported_unit = ""
            self.last_unit_reply = str(token)
            return reply          # another number, not a unit - ignore it
        if not token:
            self.reported_unit = ""
            self.last_unit_reply = ""
            return reply
        self.last_unit_reply = str(token)
        reported = unit_name_for_token(token)
        self.reported_unit = reported
        # Never replace a proven device token with an unrecognised string.
        if reported and token != self.unit_token:
            self.unit_token = token
            self.profile["unit_token"] = token
            self.log("INFO", f"{self.name}: unit token is "
                             f"{describe_unit_token(token)}")
        if reported:
            if reported != self.unit:
                self.log("WARN", f"{self.name}: instrument reports {reported}, "
                                 f"but its configured range is in {self.unit}.")
        return reply

    def read_unit(self):
        """Read and retain the heat source's current device-reported unit."""
        cmd = self._read_cmd("unit")
        if not cmd:
            return ""
        # A failed query must not leave a previous reply looking current.
        self.last_unit_reply = ""
        self.reported_unit = ""
        with self.lock:
            reply = self.link.query(cmd)
        self.last_unit_reply = str(reply or "")
        reported = unit_name_from_reply(reply)
        self.reported_unit = reported
        if reported:
            token = unit_token_from_reply(reply)
            if token:
                self.unit_token = token
                self.profile["unit_token"] = token
            if reported != self.unit:
                self.log("WARN", f"{self.name}: instrument reports {reported}, "
                                 f"but its configured range is in {self.unit}.")
        return reported

    def refresh_reported_unit(self):
        """Obtain current unit evidence without trusting profile metadata."""
        unit_cmd = self._read_cmd("unit")
        if unit_cmd:
            # The activity log showed one empty network reply immediately
            # after output-enable, followed by normal replies on retry.  Make
            # one fresh read-only retry for an empty/failed exchange.  Never
            # reuse cached unit evidence, and never hide a non-empty but
            # unrecognised/conflicting device reply behind a retry.
            for attempt in range(2):
                try:
                    reported = self.read_unit()
                except Exception:
                    if attempt:
                        raise
                    time.sleep(0.1)
                    continue
                if reported or self.last_unit_reply:
                    return reported
                if not attempt:
                    time.sleep(0.1)
            return ""
        sp_read = self._read_cmd("sp_read")
        if sp_read:
            self.last_unit_reply = ""
            self.reported_unit = ""
            with self.lock:
                reply = self.link.query(sp_read)
            self.last_setpoint_readback_raw = str(reply or "")
            self._capture_unit(reply)
        return self.reported_unit

    @property
    def effective_unit_token(self):
        """The token to substitute for {unit}: reported, stored, or mapped."""
        return (self.unit_token
                or str(self.profile.get("unit_token") or "")
                or unit_token_for(self.unit))

    def format_setpoint_command(self, template, value):
        """Fill {value} and, when required, {unit} in a write template."""
        # Preserve the configured numeric value instead of hard-rounding every
        # command to two decimals.  Instrument readback remains authoritative.
        out = template.replace("{value}", format(float(value), ".12g"))
        if "{unit}" in out:
            token = self.effective_unit_token
            if not token:
                raise RuntimeError(
                    f"{self.name}: this set-point command needs a unit, but "
                    "none is known yet. Read the set point once (or run "
                    "Read-only check / discover) so the instrument can report "
                    "it.")
            out = out.replace("{unit}", token)
        return out

    def read_setpoint(self):
        cmd = self._read_cmd("sp_read")
        if not cmd:
            return None
        with self.lock:
            reply = self.link.query(cmd)
        self.last_setpoint_readback_raw = str(reply or "")
        token = second_field(reply)
        token_is_text = False
        if token:
            try:
                float(token)
            except ValueError:
                token_is_text = True
        two_field_reply = len(str(reply or "").split(",")) == 2
        self.last_setpoint_readback_unit_raw = (
            token if (plausible_unit_token(token) or token_is_text or
                      (token and two_field_reply)) else "")
        value = first_float(self._capture_unit(reply))
        self.last_setpoint_readback_unit = self.reported_unit
        return value

    def read_temperature(self):
        """The heat source's own block/control sensor (not the reference)."""
        cmd = self._read_cmd("value")
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
        if self.reported_unit and self.reported_unit != self.unit:
            raise RuntimeError(
                f"{self.name} reports {self.reported_unit}, but its configured "
                f"range is in {self.unit}. Nothing was sent.")
        with self.lock:
            if "{unit}" in tmpl and not self.effective_unit_token:
                # Ask the instrument what unit it wants before writing.
                sp_read = self._read_cmd("sp_read")
                try:
                    if sp_read:
                        self._capture_unit(self.link.query(sp_read))
                except Exception:
                    pass
            command = self.format_setpoint_command(tmpl, value)
            self.last_setpoint_command = command
            pw = self._cmd("password")
            if send_password and pw:
                self.link.write(pw)
                time.sleep(0.1)
            self.link.write(command)
        self.log("INFO", f"{self.name}: set point -> {value:g} {self.unit}")
        return True

    def confirm_setpoint(self, value, tolerance=0.05, settle=0.4):
        """Read the set point back. Returns (ok, readback)."""
        if not self._read_cmd("sp_read"):
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

    def _query_candidate(self, command):
        """Try one discovery query without writing or inspecting error state."""
        exact = str(command or "").strip()
        if not is_safe_read_command(exact):
            return (False, "")
        try:
            reply = self.link.query(exact)
        except Exception:
            return (False, "")
        return (bool(reply), reply)

    def _paired_setpoint_write(self, read_command):
        """Infer, but never send, the write paired with a proven read query."""
        write = WRITE_PAIRS.get(read_command)
        if (write and self.profile.get("family") == "additel_well"
                and self.unit_token and "{unit}" not in write
                and "TARG" in write.upper()):
            write += ",{unit}"
        return write

    def sweep(self, kind, log=None):
        """Try every candidate for one field and adopt the first that works.

        Used from the Terminal when a command could not be found
        automatically. Strictly read-only: only queries are sent; discovery
        does not clear or consume the device's error queue.
        """
        say = log or (lambda tag, msg: self.log(tag, msg))
        lists = {"value": VALUE_CANDIDATES, "sp_read": SP_READ_CANDIDATES,
                 "unit": UNIT_CANDIDATES}
        labels = {"value": "current temperature", "sp_read": "set point",
                  "unit": "unit"}
        candidates = lists.get(kind)
        if candidates is None:
            raise ValueError(f"Nothing to sweep for {kind!r}.")
        if not self.is_open:
            raise RuntimeError(f"{self.name} is not connected.")
        family_name = self.profile.get("family") or "unknown"
        if family_name not in ("generic_scpi", "unknown"):
            family = FAMILIES.get(family_name, {})
            family_command = (family.get("commands") or {}).get(kind)
            candidates = (family_command,) if family_command else ()
        else:
            say("WARN", "This unknown/generic search may cause the instrument "
                        "to record rejected queries as diagnostic errors. No "
                        "error-queue command will be sent, and the queue will "
                        "not be cleared or consumed.")
        parser = (lambda r: (r or "").strip()) if kind == "unit" else first_float
        results, winner = [], None
        write_inferred = False
        say("INFO", "Read-only check / discover: no set point, control, "
                    "password, *CLS, or error-queue command will be sent.")
        with self.lock:
            for cmd in candidates:
                accepted, reply = self._query_candidate(cmd)
                good = accepted and parser(reply) not in (None, "")
                results.append((cmd, reply, good))
                say("PASS" if good else "INFO",
                    f"{cmd}  ->  {reply!r}" + ("" if good else "   (no)"))
                if good and winner is None:
                    winner = cmd
                    if kind in ("sp_read", "value"):
                        self._capture_unit(reply)
        if winner:
            self.profile[kind] = winner
            if kind == "sp_read":
                paired = self._paired_setpoint_write(winner)
                if paired and not self._cmd("sp_write"):
                    self.profile["sp_write"] = paired
                    write_inferred = True
                    say("INFO", f"Inferred set-point write {paired!r} from "
                                f"the read query. It was not sent; the "
                                "device set point is unchanged.")
            say("PASS", f"Adopted {winner!r} as the "
                        f"{labels[kind]} command.")
        else:
            say("FAIL", f"No candidate worked for the {labels[kind]}. "
                        "Check Additel's programming-commands PDF and type "
                        "the command here to test it.")
        # A read-only sweep can discover syntax, but cannot verify a write,
        # including when none of the read candidates answers.
        self.profile["verified"] = False
        return {"winner": winner, "results": results, "read_only": True,
                "write_inferred": write_inferred, "verified": False}

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
        return error_code_from_reply(reply)

    def _has_error_queue(self):
        """True if the instrument reports errors for bad commands.

        Additel documents SYSTem:ERRor? as the way to check whether a control
        command was accepted, which makes discovery far more reliable than
        guessing from replies. Confirm it works by sending deliberate
        nonsense and checking that it complains.
        """
        try:
            self.link.write("*CLS")
            baseline, _ = self._error_code()
            if baseline != 0:
                return False
            # A bad header produces an error-queue entry, not a query reply.
            # Sending it with query() needlessly waits for a response and was
            # the source of a stale -110 when that wait failed early.
            self.link.write(NONSENSE_COMMAND)
            probe, _ = self._error_code()
            if probe in (None, 0):
                return False
            empty, _ = self._error_code()
            return empty == 0
        except Exception:
            return False
        finally:
            try:
                self.link.write("*CLS")
            except Exception:
                pass

    def _accepted(self, command, use_errors, expect_reply=True):
        """Send one candidate. Returns (accepted, reply)."""
        try:
            reply, error_reply = checked_exchange(
                self.link, command, expect_reply=expect_reply,
                check_error=use_errors)
        except Exception:
            return (False, "")
        if use_errors:
            code, _text = error_code_from_reply(error_reply)
            if code is not None:
                return (code == 0, reply)
        # No usable error queue: a parseable reply is the only evidence.
        return (bool(reply), reply)

    def verify_commands(self, test_delta=0.5, restore=True, log=None):
        """Discover command syntax using device queries and nothing else.

        ``test_delta`` and ``restore`` remain for API compatibility but cannot
        enable a write.  Set-point write syntax may be inferred from a proven
        read query; it is deliberately not exercised here.  Discovery also
        never clears, consumes, or deliberately probes the device error queue.
        """
        say = log or (lambda tag, msg: self.log(tag, msg))
        if not self.is_open:
            raise RuntimeError(f"{self.name} is not connected.")
        report = {"idn": "", "adopted": {}, "failed": [], "verified": False,
                  "error_queue": False, "tried": [], "read_only": True,
                  "write_inferred": False, "write_command": ""}

        with self.lock:
            report["idn"] = self.idn or self.link.query("*IDN?")
            say("INFO", f"{self.name} identifies as: "
                        f"{report['idn'] or '(no reply)'}")
            say("INFO", "Read-only check / discover is active: only safe "
                        "device queries will be sent. The set point, control "
                        "state, password, and error queue will not be written, "
                        "cleared, or inspected.")

            family_name = self.profile.get("family") or "unknown"
            fam = FAMILIES.get(family_name, {})
            fam_cmds = fam.get("commands") or {}
            broad_discovery = family_name in ("generic_scpi", "unknown")
            if broad_discovery:
                report["may_log_rejected_queries"] = True
                say("WARN", "This profile has no authoritative command set. "
                            "Only query forms will be sent, but the instrument "
                            "may record unsupported queries as diagnostic "
                            "errors; the suite will not clear or consume them.")

            def order(key, generic):
                seen, out = set(), []
                if broad_discovery:
                    choices = [self._cmd(key)]
                    choices += list(generic)
                else:
                    choices = [fam_cmds.get(key)]
                for cmd in choices:
                    if cmd and cmd not in seen:
                        seen.add(cmd)
                        out.append(cmd)
                return out

            def try_candidates(label, candidates, parser):
                for cmd in candidates:
                    accepted, reply = self._query_candidate(cmd)
                    report["tried"].append((label, cmd, reply))
                    value = parser(reply) if reply else None
                    if accepted and value is not None and value != "":
                        say("PASS", f"{label}: {cmd!r} works (reply {reply!r})")
                        return cmd, value, reply
                    say("INFO", f"{label}: {cmd!r} - no")
                say("FAIL", f"{label}: nothing worked.")
                report["failed"].append(label)
                return None, None, ""

            sp_read, original, sp_reply = try_candidates(
                "Set-point read", order("sp_read", SP_READ_CANDIDATES),
                first_float)
            if sp_read:
                report["adopted"]["sp_read"] = sp_read
                self._capture_unit(sp_reply)
                if self.unit_token:
                    report["unit_token"] = self.unit_token
                    say("INFO", f"The set point is reported with a unit "
                                f"({describe_unit_token(self.unit_token)}), "
                                "so an inferred Additel target write will "
                                "retain that device-supplied unit token.")

            value_cmd, _, _ = try_candidates(
                "Value (block temperature)", order("value", VALUE_CANDIDATES),
                first_float)
            if value_cmd:
                report["adopted"]["value"] = value_cmd

            unit_cmd, _, _ = try_candidates(
                "Unit read", order("unit", UNIT_CANDIDATES),
                lambda r: (r or "").strip())
            if unit_cmd:
                report["adopted"]["unit"] = unit_cmd

            status_queries = CONTROL_STATUS_CANDIDATES.get(family_name, ())
            if status_queries:
                for query in status_queries:
                    accepted, reply = self._query_candidate(query)
                    report["tried"].append(("Control status", query, reply))
                    if accepted and reply:
                        report["control_status_query"] = query
                        report["control_status_reply"] = reply
                        say("PASS", f"Control status: {query!r} answers "
                                    f"{reply!r}. This read does not imply or "
                                    "send an enable/disable command.")
                        break
                else:
                    say("INFO", "No read-only control-status query answered. "
                                "No control command was inferred or sent.")
            elif broad_discovery:
                for query, (on_cmd, off_cmd) in CONTROL_PAIRS.items():
                    accepted, reply = self._query_candidate(query)
                    report["tried"].append(("Output control", query, reply))
                    if accepted and reply:
                        report["adopted"]["enable"] = on_cmd
                        report["adopted"]["disable"] = off_cmd
                        say("PASS", f"Output control: read-only query "
                                    f"{query!r} answers {reply!r}; inferred "
                                    f"{on_cmd!r} / {off_cmd!r}, neither sent.")
                        break
                else:
                    say("INFO", "No read-only output-status query answered. "
                                "No output command was inferred or sent.")
            else:
                say("INFO", "No control-status query is defined for this "
                            "instrument family; none was sent.")

            stability_candidates = STABLE_CANDIDATES if broad_discovery else ()
            for cmd in stability_candidates:
                accepted, reply = self._query_candidate(cmd)
                report["tried"].append(("Stability", cmd, reply))
                if accepted and reply:
                    say("INFO", f"Instrument also reports its own stability "
                                f"via {cmd!r} ({reply!r}). The suite still "
                                "judges stability from the reference probe.")
                    break

            # Pair a proven read with a known write template, but do not send
            # it. The first run both sends and immediately reads it back.
            if sp_read and original is not None:
                stored_write = self._cmd("sp_write")
                family_write = fam_cmds.get("sp_write") or ""
                paired_write = self._paired_setpoint_write(sp_read)
                write = stored_write or family_write or paired_write
                if not write:
                    say("FAIL", f"Set-point write: no known pairing for "
                                f"{sp_read!r}; enter it by hand.")
                    report["failed"].append("Set-point write")
                else:
                    inferred = not stored_write and not family_write
                    report["adopted"]["sp_write"] = write
                    report["write_command"] = write
                    report["write_inferred"] = inferred
                    if inferred:
                        say("INFO", f"Set-point write: inferred {write!r} "
                                    f"from {sp_read!r}. It was not sent; the "
                                    f"device target remains {original:g}.")
                    else:
                        say("INFO", f"Set-point write: retained {write!r} as "
                                    "a candidate. It was not sent or verified; "
                                    f"the device target remains {original:g}.")

        self.profile.update(report["adopted"])
        # Query-only discovery cannot verify any command that changes state.
        self.profile["verified"] = False
        if not report["failed"]:
            say("PASS", f"{self.name}: read-only check / discover complete. "
                        "No set point or control state was changed; the write "
                        "candidate remains unverified until a run commands "
                        "and reads back its first requested point.")
        return report
