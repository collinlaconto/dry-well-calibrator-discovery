"""ADT286 client — one shared scanner serving several concurrent runs.

Why this design
---------------
The 286 has a single scan configuration: SCAN:MULT:STARt takes one channel
list. So concurrent runs cannot each start their own scan. Instead every run
*subscribes* its channels here; this client keeps the union of all subscribed
channels in one scan and fans the readings out by channel name. One poll
serves everyone.

Commands used (from Additel's published ADT286 command set):
    *IDN?                          identity
    MODule:INFormation?            module/box inventory
    MODule:CONFig? <index>         channel names + configuration per module
    CHANnel:CONFig? "<name>"       one channel's configuration
    SCAN:MULT:STARt <rate>,"<ch,ch>"   configure + start multi-channel scan
    SCAN:DATA:Last? 1              most recent scan data + device timestamp
    SCAN:STOP                      stop scanning
    UNIT:TEMPerature?              system temperature unit
    SYSTem:ERRor?                  error queue

Reply format for SCAN:DATA:Last? (per channel, groups split by ';'):
    name, elecUnitId, n, <2n electrical values>, indUnitId, m, <m values>[, CJC...]
e.g. "REF1,1281,1,28.258167,28.258167,1001,1,33.512077;"
"""

import math
import re
import threading
import time
from dataclasses import dataclass

from .transport import (describe_target, make_link,
                        normalize_target, target_is_set)

TEMP_UNIT_IDS = {1000: "K", 1001: "°C", 1002: "°F", 1003: "°R", 999: "°Re"}
ELEC_UNIT_IDS = {1281: "Ω", 1284: "kΩ", 1283: "MΩ", 1243: "mV", 1240: "V",
                 1211: "mA", 1212: "µA", 1209: "A"}

# channel type ids from the command set (4th field of a channel config)
CHANNEL_TYPES = {
    0: "voltage", 1: "current", 2: "resistance", 3: "RTD", 4: "thermistor",
    100: "thermocouple", 101: "switch", 102: "SPRT", 103: "voltage transmitter",
    104: "current transmitter", 105: "standard TC", 106: "custom RTD",
    110: "standard resistance",
}

SCAN_RATES = ("100", "1000", "4000")
DEVICE_TIME_RE = re.compile(
    r"\d{4}:\d{2}:\d{2}\s+\d{2}:\d{2}:\d{2}(?:[ .:]\d{1,9})?")


@dataclass(frozen=True)
class Reading:
    """One immutable, device-sourced channel reading.

    ``timestamp`` and ``monotonic`` are host receipt metadata.
    ``device_timestamp`` is the exact acquisition-time token requested from
    the ADT286 when its firmware supplies that optional field.  It remains
    empty when omitted; host receipt time is retained separately and is never
    presented as device time.  Numeric tokens reported by the device are kept
    separately so raw exports do not lose resolution through display
    formatting.
    """

    channel: str
    temperature: object = None
    unit: object = None
    electrical: object = None
    electrical_unit: object = None
    cycle: int = 0
    timestamp: float = 0.0
    monotonic: float = 0.0
    device_timestamp: str = ""
    raw_temperature: str = ""
    raw_electrical: str = ""
    source: str = "ADT286 SCAN:DATA:Last? 1"

    def __repr__(self):                                   # pragma: no cover
        return (f"<Reading {self.channel} {self.temperature}{self.unit or ''} "
                f"cycle={self.cycle}>")


def parse_scan_data(payload):
    """Parse a SCAN:DATA:Last? reply into {channel_name: dict}.

    Tolerant by design: unknown trailing fields (thermocouple cold-junction
    blocks, switch status) are ignored rather than breaking the parse.
    """
    out = {}
    if not payload:
        return out
    clean_payload = payload.strip().strip('"')
    all_device_times = DEVICE_TIME_RE.findall(clean_payload)
    shared_device_time = (all_device_times[0]
                          if len(set(all_device_times)) == 1 else "")
    parsed_order = []
    for group in clean_payload.split(";"):
        group = group.strip()
        if not group:
            continue
        group_device_times = DEVICE_TIME_RE.findall(group)
        device_time = (group_device_times[0]
                       if len(set(group_device_times)) == 1 else "")
        fields = []
        for raw_field in group.split(","):
            field = raw_field.strip().strip('"')
            # Additel documents the timestamp format, but does not promise
            # that it occupies its own comma-delimited field.  Some firmware
            # places it directly against the opening/closing quote or the
            # first/last data token.  Remove only the time substring so an
            # adjacent channel name or measurement is never discarded.
            carried_device_time = DEVICE_TIME_RE.search(field) is not None
            field = DEVICE_TIME_RE.sub("", field).strip().strip('"').strip()
            if field or not carried_device_time:
                fields.append(field)
        device_time = device_time or shared_device_time
        f = fields
        if len(f) < 3 or not f[0]:
            continue
        name = f[0]
        try:
            elec_unit = int(f[1])
            n_elec = int(f[2])
        except ValueError:
            continue
        elec = None
        raw_elec = f[3] if len(f) > 3 else ""
        if raw_elec:
            try:
                candidate = float(raw_elec)
                elec = candidate if math.isfinite(candidate) else None
            except ValueError:
                elec = None
        temp = t_unit = None
        raw_temp = ""
        idx = 3 + 2 * n_elec              # raw + filtered per electrical point
        if len(f) > idx + 1:
            try:
                unit_id = int(f[idx])
                m = int(f[idx + 1])
                if m >= 1 and len(f) > idx + 2:
                    raw_temp = f[idx + 2]
                if unit_id in TEMP_UNIT_IDS and raw_temp:
                    candidate = float(raw_temp)
                    temp = candidate if math.isfinite(candidate) else None
                    t_unit = TEMP_UNIT_IDS[unit_id]
            except (ValueError, IndexError):
                pass
        out[name] = {"temperature": temp, "unit": t_unit, "electrical": elec,
                     "electrical_unit": ELEC_UNIT_IDS.get(elec_unit,
                                                          str(elec_unit)),
                     "raw_temperature": raw_temp,
                     "raw_electrical": raw_elec,
                     "device_timestamp": device_time}
        parsed_order.append(name)

    # A single time token applies to the whole returned scan.  If firmware
    # returns one timestamp per channel outside the channel's comma fields,
    # preserve the documented response order rather than withholding an
    # otherwise complete device frame.
    if (not shared_device_time and len(all_device_times) == len(parsed_order)):
        for name, device_time in zip(parsed_order, all_device_times):
            if not out[name]["device_timestamp"]:
                out[name]["device_timestamp"] = device_time
    return out


def parse_module_info(payload):
    """Parse MODule:INFormation? -> [{index, serial, type, channels, label}]."""
    mods = []
    for group in (payload or "").split(";"):
        f = [x.strip() for x in group.split(",")]
        if len(f) < 6 or not f[0]:
            continue
        try:
            idx = int(f[0])
            count = int(f[5]) if f[5] else 0
        except ValueError:
            continue
        try:
            btype = int(f[2]) if f[2] else 0
        except ValueError:
            btype = 0
        mods.append({"index": idx, "serial": f[1], "type": btype,
                     "channels": count, "label": f[6] if len(f) > 6 else ""})
    return mods


def synth_channel_names(module_index, box_type, count):
    """Channel names implied by a module's type and channel count.

    Front panel (index 0) -> REF1, REF2
    Temperature box       -> CHx-01A..10A then CHx-01B..10B
    Process box           -> CHx-01..CHx-10
    """
    if module_index == 0:
        return ["REF1", "REF2"][:max(0, count)]
    if box_type == 2:
        return [f"CH{module_index}-{i:02d}" for i in range(1, count + 1)]
    names = [f"CH{module_index}-{i:02d}A" for i in range(1, min(count, 10) + 1)]
    if count > 10:
        names += [f"CH{module_index}-{i:02d}B"
                  for i in range(1, count - 10 + 1)]
    return names


def parse_channel_names(payload):
    """First field of every ';' group of a MODule:CONFig? reply."""
    names = []
    for group in (payload or "").split(";"):
        f = group.split(",")
        if f and f[0].strip():
            names.append(f[0].strip())
    return names


def parse_channel_config(payload):
    """Parse a channel config reply -> {name, enabled, label, type, sensor}."""
    f = [x.strip() for x in (payload or "").split(",")]
    if len(f) < 4 or not f[0]:
        return None
    try:
        ctype = int(f[3])
    except ValueError:
        ctype = -1
    sensor = ""
    # RTD/SPRT/TC configs carry the sensor name shortly after the 8 common
    # fields; index 9 in the documented examples.
    if len(f) > 9 and f[9] and not f[9].isdigit():
        sensor = f[9]
    sensor_serial = f[10] if len(f) > 10 else ""
    return {"name": f[0], "enabled": f[1] == "1", "label": f[2],
            "type": ctype, "type_name": CHANNEL_TYPES.get(ctype, f"type {ctype}"),
            "sensor": sensor, "serial": sensor_serial,
            "raw": str(payload or "").strip()}


class Adt286:
    """Thread-safe shared client for one ADT286."""

    def __init__(self, logger=None):
        self.log = logger or (lambda tag, msg: None)
        self.link = None
        self.lock = threading.RLock()          # guards the serial link
        self.idn = ""
        self.unit = ""
        self.channels = []                     # discovered channel names
        self.channel_info = {}                 # name -> config dict
        self.scan_rate = "1000"
        self._subs = {}                        # owner -> [channels]
        self._readings = {}                    # channel -> Reading
        self._cycle = 0
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self.poll_interval = 1.0
        self.last_error = ""
        # Scan watchdog. The 286 keeps ONE scan configuration, and moving to
        # another function on its display tears that scan down -- readings
        # simply stop. Rather than going blind, notice and re-establish it.
        self.recover_after = 3          # consecutive bad polls before acting
        self.recover_min_gap = 5.0      # seconds between recovery attempts
        self._bad_polls = 0
        self._last_recovery = 0.0
        self.recoveries = 0
        self.on_recovery = None         # optional callback(message)
        self._last_poll_started = 0.0
        self._last_device_times = {}
        self.freshness_supported = None

    # ------------------------------------------------------------ session --
    @property
    def is_open(self):
        return self.link is not None and self.link.is_open

    def connect(self, target, baud=9600):
        """Connect over USB/serial or the network (Ethernet / Wi-Fi)."""
        if self.is_open:
            raise RuntimeError("The ADT286 is already connected.")
        t = normalize_target(target, str(baud))
        if not target_is_set(t):
            raise RuntimeError("No port or address given for the ADT286.")
        with self.lock:
            self.link = make_link(
                t, terminator="\r\n", reply_timeout=2.0,
                # The command set specifies the quoted/semicolon scan body
                # but does not mandate a wire terminator.  Accept an idle-
                # completed body; poll_once still requires every subscribed
                # channel before publishing a complete frame.
                require_reply_terminator=False, max_reply_time=30.0)
            self.link.open(t)
            self.idn = self.link.query("*IDN?")
            if not self.idn:
                self.link.close()
                raise RuntimeError(
                    "No reply to *IDN? on " + describe_target(t) + ". Over "
                    "USB, check the Additel USB driver is installed; over the "
                    "network, check the address and port (use 'Find port').")
            unit_reply = self.link.query("UNIT:TEMPerature?")
            self.unit = self._unit_from(unit_reply)
        self.log("PASS", f"ADT286 connected on {describe_target(t)}: "
                         f"{self.idn} (unit {self.unit})")
        self.discover_channels()
        self._start_poller()
        return self.idn

    @staticmethod
    def _unit_from(reply):
        """UNIT:TEMPerature? returns 'name,id'."""
        if not reply:
            return ""
        parts = [p.strip() for p in reply.split(",")]
        for p in reversed(parts):
            if p.isdigit() and int(p) in TEMP_UNIT_IDS:
                return TEMP_UNIT_IDS[int(p)]
        return parts[0].strip('"') if parts else ""

    def disconnect(self):
        self._stop_poller()
        with self.lock:
            if self.link is not None and self.link.is_open:
                try:
                    self.link.write("SCAN:STOP")
                except Exception:
                    pass
            if self.link is not None:
                self.link.close()
        with self.lock:
            self._subs.clear()
            self._readings.clear()
            self._last_device_times.clear()
            self.freshness_supported = None
        self.log("INFO", "ADT286 disconnected.")

    # ----------------------------------------------------------- channels --
    def discover_channels(self):
        """Enumerate available channels from the module inventory."""
        names, info = [], {}
        with self.lock:
            modules = parse_module_info(self.link.query("MODule:INFormation?"))
            for mod in modules:
                reply = self.link.query(f"MODule:CONFig? {mod['index']}")
                found = parse_channel_names(reply)
                if not found:
                    found = synth_channel_names(mod["index"], mod["type"],
                                                mod["channels"])
                for group in (reply or "").split(";"):
                    cfg = parse_channel_config(group)
                    if cfg:
                        info[cfg["name"]] = cfg
                names.extend(n for n in found if n not in names)
        self.channels = names
        self.channel_info = info
        self.log("INFO", f"ADT286 channels: {', '.join(names) or '(none)'}")
        return names

    def describe(self, channel):
        cfg = self.channel_info.get(channel)
        if not cfg:
            return channel
        bits = [cfg["type_name"]]
        if cfg.get("sensor"):
            bits.append(cfg["sensor"])
        if not cfg.get("enabled", True):
            bits.append("DISABLED on the 286")
        return f"{channel} ({', '.join(bits)})"

    # ------------------------------------------------------ subscriptions --
    def subscribe(self, owner, channels):
        """Add an owner's channels to the shared scan."""
        with self.lock:
            self._subs[owner] = list(channels)
            try:
                self._restart_scan(strict=True)
            except Exception:
                self._subs.pop(owner, None)
                raise

    def unsubscribe(self, owner):
        with self.lock:
            self._subs.pop(owner, None)
            self._restart_scan()

    def subscribed_channels(self):
        with self.lock:
            seen = []
            for chans in self._subs.values():
                for c in chans:
                    if c not in seen:
                        seen.append(c)
            return seen

    @property
    def scan_period(self):
        """Configured device scan period in seconds."""
        try:
            return max(0.1, float(self.scan_rate) / 1000.0)
        except (TypeError, ValueError):
            return 1.0

    @property
    def minimum_poll_interval(self):
        """Fastest safe query interval for SCAN:DATA:Last?."""
        return self.scan_period

    def _restart_scan(self, strict=False):
        """Reconfigure the single shared scan for the union of subscribers."""
        chans = self.subscribed_channels()
        if self.link is None or not self.link.is_open:
            if strict:
                raise RuntimeError("The ADT286 connection is not open.")
            return False
        if not chans:
            try:
                self.link.write("SCAN:STOP")
                self.log("INFO", "No subscribers left; scanning stopped.")
                return True
            except Exception as e:
                self.log("WARN", f"SCAN:STOP failed: {e}")
                if strict:
                    raise
                return False
        cmd = f'SCAN:MULT:STARt {self.scan_rate},"{",".join(chans)}"'
        try:
            self.link.write(cmd)
            # Anything cached predates this scan configuration.  Clearing it
            # prevents the UI or a newly-started run from presenting it as a
            # reading from the reconfigured scan.
            for channel in chans:
                self._readings.pop(channel, None)
            self._last_poll_started = 0.0
            self._bad_polls = 0
            self.log("INFO", f"Scanning {len(chans)} channel(s): "
                             f"{', '.join(chans)}")
            return True
        except Exception as e:
            self.log("FAIL", f"Could not start scan: {e}")
            if strict:
                raise
            return False

    # ---------------------------------------------------------- polling ----
    def _start_poller(self):
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop,
                                             daemon=True)
        self._poll_thread.start()

    def _stop_poller(self):
        self._poll_stop.set()
        t = self._poll_thread
        self._poll_thread = None
        if t and t.is_alive():
            t.join(timeout=3.0)

    def _note_bad_poll(self, reason):
        """Count a poll that produced nothing usable; recover if persistent."""
        self._bad_polls += 1
        if self._bad_polls < self.recover_after:
            return False
        now = time.time()
        if now - self._last_recovery < self.recover_min_gap:
            return False
        self._last_recovery = now
        message = (f"The 286 stopped returning scan data ({reason}). This "
                   "normally means its display was switched to another "
                   "function, which cancels the scan. Re-establishing it - "
                   "runs in progress will carry on.")
        self.log("WARN", message)
        recovered = self._restart_scan()
        if recovered:
            self.recoveries += 1
        if recovered and self.on_recovery:
            try:
                self.on_recovery(message)
            except Exception:
                pass
        if recovered:
            self._bad_polls = 0
            return True
        self.log("FAIL", "The ADT286 scan could not be re-established on the "
                         "current connection.")
        return False

    def poll_once(self):
        """One scan read. Returns the number of channels updated."""
        requested = self.subscribed_channels()
        if self.link is None or not self.link.is_open or not requested:
            return 0
        with self.lock:
            # Request Additel's optional device timestamp and avoid querying
            # faster than the configured scan period.  Firmware that supplies
            # timestamps gets physical-scan de-duplication.  Firmware that
            # omits the optional field still publishes the exact device values
            # with an honestly blank device_timestamp and separate host receipt
            # metadata.
            started = time.monotonic()
            if started - self._last_poll_started < self.minimum_poll_interval:
                return 0
            self._last_poll_started = started
            try:
                payload = self.link.query("SCAN:DATA:Last? 1")
            except Exception as e:
                self.last_error = str(e)
                self._note_bad_poll("the connection returned an error")
                return 0
            data = parse_scan_data(payload)
            if not data:
                self._note_bad_poll("no readings came back")
                return 0
            missing_channels = [c for c in requested if c not in data]
            if missing_channels:
                for channel in requested:
                    self._readings.pop(channel, None)
                self.last_error = (
                    "ADT286 returned an incomplete scan frame; missing "
                    + ", ".join(missing_channels))
                self._note_bad_poll(
                    "an incomplete response omitted "
                    + ", ".join(missing_channels))
                return 0
            missing_device_times = [
                c for c in requested
                if not data[c].get("device_timestamp")]
            unusable = [c for c in requested
                        if c not in data or data[c]["temperature"] is None]
            if unusable:
                # A calibration frame is atomic: never publish the valid
                # subset when any requested channel is unusable.  Otherwise a
                # reference-only consumer could count an old/partial physical
                # scan while the DUT evidence for that cycle is absent.
                self._note_bad_poll(
                    f"{len(unusable)} subscribed channel(s) missing or invalid "
                    "in the scan")
                for channel in requested:
                    self._readings.pop(channel, None)
                return 0
            else:
                # Only a complete, usable frame may change timestamp mode or
                # freshness history.  A rejected frame must never erase the
                # evidence needed to identify an old timestamped frame later.
                if missing_device_times:
                    for channel in requested:
                        self._last_device_times.pop(channel, None)
                    if self.freshness_supported is not False:
                        self.log(
                            "WARN", "This ADT286 firmware omitted the optional "
                            "device timestamp from SCAN:DATA:Last? 1. Complete "
                            "device readings will be recorded; device-time "
                            "fields remain blank and host receipt time is kept "
                            "separately."
                        )
                    self.freshness_supported = False
                else:
                    if self.freshness_supported is False:
                        self.log(
                            "INFO", "ADT286 device timestamps are available "
                            "again; physical-scan de-duplication resumed.")
                    self.freshness_supported = True
                    repeated_times = [
                        c for c in requested
                        if self._last_device_times.get(c) ==
                        data[c].get("device_timestamp")]
                    if repeated_times:
                        self._note_bad_poll(
                            "the device acquisition timestamp did not advance "
                            "for " + ", ".join(repeated_times))
                        return 0
                self._bad_polls = 0
                self.last_error = ""
            self._cycle += 1
            now = time.time()
            acquired = time.monotonic()
            for name in requested:
                vals = data.get(name)
                if vals is None:
                    self._readings.pop(name, None)
                    continue
                self._readings[name] = Reading(
                    channel=name,
                    temperature=vals["temperature"],
                    unit=vals["unit"],
                    electrical=vals["electrical"],
                    electrical_unit=vals["electrical_unit"],
                    cycle=self._cycle,
                    timestamp=now,
                    monotonic=acquired,
                    device_timestamp=vals["device_timestamp"],
                    raw_temperature=vals["raw_temperature"],
                    raw_electrical=vals["raw_electrical"],
                )
            if self.freshness_supported:
                self._last_device_times.update(
                    {name: data[name]["device_timestamp"]
                     for name in requested})
            reported_units = {data[c]["unit"] for c in requested
                              if c in data and data[c]["temperature"] is not None
                              and data[c]["unit"]}
            if len(reported_units) == 1:
                reported = next(iter(reported_units))
                if reported != self.unit:
                    self.log("WARN", f"ADT286 scan unit changed from "
                                     f"{self.unit or '(unknown)'} to {reported}.")
                    self.unit = reported
            return len(requested) - len(unusable)

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            try:
                self.poll_once()
            except Exception as e:                        # pragma: no cover
                self.last_error = str(e)
            self._poll_stop.wait(max(self.poll_interval,
                                     self.minimum_poll_interval))

    # ---------------------------------------------------------- readings ---
    def latest(self, channel):
        with self.lock:
            return self._readings.get(channel)

    def snapshot(self, channels, cycle=None):
        """Return immutable readings, optionally restricted to one cycle.

        A channel that is absent or belongs to a different cycle maps to
        ``None``.  Callers never receive a mixed-cycle sample by accident.
        """
        with self.lock:
            out = {}
            for channel in channels:
                reading = self._readings.get(channel)
                out[channel] = (reading if reading is not None and
                                (cycle is None or reading.cycle == cycle)
                                else None)
            return out

    @property
    def cycle(self):
        with self.lock:
            return self._cycle

    @property
    def health(self):
        """Short description of scan health, for the run screen."""
        if self.link is None or not self.link.is_open:
            return "not connected"
        if not self.subscribed_channels():
            return "idle (no run subscribed)"
        if self._bad_polls:
            return f"no data for {self._bad_polls} poll(s) - recovering"
        base = "scanning"
        if self.freshness_supported is False:
            base += " (device time unavailable; host receipt time retained)"
        if self.recoveries:
            base += f" (recovered {self.recoveries}x)"
        return base

    def system_error(self):
        with self.lock:
            try:
                return self.link.query("SYSTem:ERRor?")
            except Exception as e:
                return str(e)
