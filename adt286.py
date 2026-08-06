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
    name, elecUnitId, n, [device time], <2n electrical values>,
    indUnitId, m, <m values>[, CJC...]
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
DRIVER_REVISION = "ADT286-2026.08.06.4-multirun-cache"
DEVICE_TIME_RE = re.compile(
    r"(?:\d{4}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2})"
    r"\s+\d{2}:\d{2}:\d{2}(?:[ .:]\d{1,9})?")


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
        self.lock = threading.RLock()          # guards blocking serial I/O
        # Cached device readings must remain available while a serial query is
        # in flight.  In particular, the Tk refresh thread must never wait for
        # SCAN:DATA:Last? (which may take up to the transport timeout).  Keep
        # short-lived subscription/cache state behind a separate lock and
        # always acquire ``lock`` before ``_state_lock`` when both are needed.
        self._state_lock = threading.RLock()
        self.idn = ""
        self.unit = ""
        self.channels = []                     # discovered channel names
        self.channel_info = {}                 # name -> config dict
        self.scan_rate = "1000"
        self._subs = {}                        # owner -> [channels]
        self._group_failures = {}              # owner -> consecutive bad frames
        self._scan_channels = ()               # configured union on the device
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
        # Record exactly one diagnostic frame for each requested channel set.
        # This is logging only: it never changes, substitutes, or rounds data.
        self._traced_scan_signatures = set()

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
            self._last_device_times.clear()
            self.freshness_supported = None
            self._traced_scan_signatures.clear()
        with self._state_lock:
            self._subs.clear()
            self._group_failures.clear()
            self._readings.clear()
        self._scan_channels = ()
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
            with self._state_lock:
                self._subs[owner] = list(channels)
                self._group_failures[owner] = 0
            try:
                self._restart_scan(strict=True)
            except Exception:
                with self._state_lock:
                    self._subs.pop(owner, None)
                    self._group_failures.pop(owner, None)
                raise

    def unsubscribe(self, owner):
        with self.lock:
            with self._state_lock:
                self._subs.pop(owner, None)
                self._group_failures.pop(owner, None)
            self._restart_scan()

    def subscribed_channels(self):
        with self._state_lock:
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
                self._scan_channels = ()
                with self._state_lock:
                    self._group_failures.clear()
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
            # Retain complete frames for channels that remain subscribed. They
            # are immutable device data with their original cycle and age, and
            # engines already require a newer cycle before using them. Clear
            # only channels newly introduced to this scan configuration so a
            # second run cannot blank the first run's live display.
            newly_added = set(chans).difference(self._scan_channels)
            with self._state_lock:
                for channel in newly_added:
                    self._readings.pop(channel, None)
                self._group_failures = {
                    owner: 0 for owner in self._subs
                }
            self._scan_channels = tuple(chans)
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

    def _note_bad_poll(self, reason, recover_after=None):
        """Count a poll that produced nothing usable; recover if persistent."""
        self._bad_polls += 1
        threshold = (self.recover_after if recover_after is None
                     else max(self.recover_after, int(recover_after)))
        if self._bad_polls < threshold:
            return False
        return self._recover_scan(reason)

    def _group_recover_after(self, channels, union_channels=None):
        """Grace for one run, based on the physical scan's entire union."""
        try:
            interval = max(float(self.poll_interval),
                           self.minimum_poll_interval, 0.1)
        except (TypeError, ValueError):
            interval = max(self.minimum_poll_interval, 0.1)
        union = tuple(union_channels or self._scan_channels or channels)
        # The 286 can acquire channels successively. Allow one configured scan
        # period per union channel plus one polling interval, but recover before
        # the engine's third consecutive 15-second acquisition timeout.
        # Leave one full poll interval after recovery for the first new query
        # to finish before the engine's third 15-second timeout.
        recovery_deadline = max(
            self.recover_after * interval,
            40.0 - interval,
        )
        grace_seconds = min(
            len(union) * self.scan_period + interval,
            recovery_deadline,
        )
        return max(self.recover_after,
                   int(math.ceil(grace_seconds / interval)))

    def _recover_scan(self, reason):
        """Re-establish the configured union without discarding cached data."""
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
        with self.lock:
            # Capture the channel union only after taking the I/O lock.  A
            # subscribe/unsubscribe operation reconfigures the device under
            # this same lock, so the response is always checked against the
            # scan configuration that actually produced it.
            requested = self.subscribed_channels()
            if self.link is None or not self.link.is_open or not requested:
                return 0
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
            signature = tuple(requested)
            trace_frame = bool(payload) and (
                signature not in self._traced_scan_signatures)
            if trace_frame:
                self.log(
                    "RX", f"ADT286 exact first parser input "
                    f"[{DRIVER_REVISION}]: {payload!r}")
            data = parse_scan_data(payload)
            if trace_frame:
                parsed = []
                for channel in requested:
                    values = data.get(channel)
                    if values is None:
                        parsed.append(f"{channel}: missing")
                        continue
                    state = ("usable" if values.get("temperature") is not None
                             else "unusable")
                    device_time = ("present" if
                                   values.get("device_timestamp") else "absent")
                    parsed.append(
                        f"{channel}: raw_temperature="
                        f"{values.get('raw_temperature', '')!r}, "
                        f"unit={values.get('unit')!r}, "
                        f"device_time={device_time}, {state}")
                self.log("INFO", "ADT286 first scan parse: "
                         + "; ".join(parsed))
                self._traced_scan_signatures.add(signature)
            if not data:
                self._note_bad_poll("no readings came back")
                return 0
            # Validate atomically per subscriber, not across unrelated runs.
            # A run may only receive a frame when its own reference and every
            # DUT are present and usable.  A bad channel in another run must
            # not blank or stall this run's wholly device-sourced frame.
            with self._state_lock:
                group_entries = []
                for owner, subscribed in self._subs.items():
                    group = tuple(dict.fromkeys(
                        channel for channel in subscribed if channel))
                    if group:
                        group_entries.append((owner, group))

            valid_entries = []
            invalid_entries = []
            for owner, group in group_entries:
                if all(channel in data and
                       data[channel]["temperature"] is not None
                       for channel in group):
                    valid_entries.append((owner, group))
                else:
                    invalid_entries.append((owner, group))

            valid_groups = [group for _owner, group in valid_entries]
            invalid_groups = [group for _owner, group in invalid_entries]

            valid_channels = list(dict.fromkeys(
                channel for group in valid_groups for channel in group))
            invalid_only_channels = set(
                channel for group in invalid_groups for channel in group
            ).difference(valid_channels)
            if invalid_only_channels:
                with self._state_lock:
                    for channel in invalid_only_channels:
                        self._readings.pop(channel, None)

            missing_channels = [c for c in requested if c not in data]
            unusable = [c for c in requested
                        if c not in data or data[c]["temperature"] is None]
            if not valid_groups:
                detail = ("missing " + ", ".join(missing_channels)
                          if missing_channels else
                          f"{len(unusable)} channel(s) were invalid")
                self.last_error = "ADT286 returned no complete run frame; " + detail
                self._note_bad_poll(detail)
                return 0

            missing_device_times = [
                channel for channel in valid_channels
                if not data[channel].get("device_timestamp")]
            if missing_device_times:
                if self.freshness_supported is not False:
                    self.log(
                        "WARN", "This ADT286 firmware omitted the optional "
                        "device timestamp from SCAN:DATA:Last? 1. Complete "
                        "device readings will be recorded; device-time fields "
                        "remain blank and host receipt time is kept separately."
                    )
                self.freshness_supported = False
            else:
                if self.freshness_supported is False:
                    self.log(
                        "INFO", "ADT286 device timestamps are available again; "
                        "physical-scan de-duplication resumed.")
                self.freshness_supported = True

            fresh_entries = []
            repeated_times = []
            for owner, group in valid_entries:
                group_times = [data[channel].get("device_timestamp")
                               for channel in group]
                if all(group_times):
                    repeated = [
                        channel for channel in group
                        if self._last_device_times.get(channel) ==
                        data[channel].get("device_timestamp")]
                    if repeated:
                        repeated_times.extend(repeated)
                        continue
                else:
                    # Timestamp support is optional.  Clear only this run's
                    # history; never substitute host time as device time.
                    for channel in group:
                        self._last_device_times.pop(channel, None)
                fresh_entries.append((owner, group))

            fresh_owners = {owner for owner, _group in fresh_entries}
            with self._state_lock:
                for owner, _group in group_entries:
                    if owner in fresh_owners:
                        self._group_failures[owner] = 0
                    else:
                        self._group_failures[owner] = (
                            self._group_failures.get(owner, 0) + 1)
                failure_counts = dict(self._group_failures)

            recovery_entries = [
                (owner, group) for owner, group in group_entries
                if failure_counts.get(owner, 0) >=
                self._group_recover_after(group, requested)
            ]

            fresh_channels = list(dict.fromkeys(
                channel for _owner, group in fresh_entries for channel in group))
            if not fresh_channels:
                # A multi-channel scan may update its channels successively.
                # Allow at least one poll per subscribed channel before the
                # watchdog restarts it, or a larger two-run scan can be reset
                # forever just before its complete frame becomes fresh.
                self._note_bad_poll(
                    "the device acquisition timestamp did not advance for "
                    + ", ".join(dict.fromkeys(repeated_times)),
                    recover_after=min(
                        self._group_recover_after(group, requested)
                        for _owner, group in group_entries))
                return 0

            self._bad_polls = 0
            if invalid_groups:
                self.last_error = (
                    "ADT286 omitted or invalidated "
                    + ", ".join(sorted(invalid_only_channels))
                    + "; other complete run frame(s) remain live")
            else:
                self.last_error = ""

            now = time.time()
            acquired = time.monotonic()
            # Publish all complete run frames from this response atomically.
            # Other run groups retain their preceding complete cycle until
            # their own full set of device timestamps advances.
            with self._state_lock:
                self._cycle += 1
                cycle = self._cycle
                for name in fresh_channels:
                    vals = data[name]
                    self._readings[name] = Reading(
                        channel=name,
                        temperature=vals["temperature"],
                        unit=vals["unit"],
                        electrical=vals["electrical"],
                        electrical_unit=vals["electrical_unit"],
                        cycle=cycle,
                        timestamp=now,
                        monotonic=acquired,
                        device_timestamp=vals["device_timestamp"],
                        raw_temperature=vals["raw_temperature"],
                        raw_electrical=vals["raw_electrical"],
                    )
            self._last_device_times.update({
                name: data[name]["device_timestamp"]
                for name in fresh_channels
                if data[name].get("device_timestamp")
            })
            reported_units = {data[c]["unit"] for c in fresh_channels
                              if data[c]["unit"]}
            if len(reported_units) == 1:
                reported = next(iter(reported_units))
                if reported != self.unit:
                    self.log("WARN", f"ADT286 scan unit changed from "
                                     f"{self.unit or '(unknown)'} to {reported}.")
                    self.unit = reported
            if recovery_entries:
                owners = ", ".join(owner for owner, _group in recovery_entries)
                self._recover_scan(
                    "complete device frames stayed unavailable for " + owners)
            return len(fresh_channels)

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
        with self._state_lock:
            return self._readings.get(channel)

    def snapshot(self, channels, cycle=None):
        """Return immutable readings, optionally restricted to one cycle.

        A channel that is absent or belongs to a different cycle maps to
        ``None``.  Callers never receive a mixed-cycle sample by accident.
        """
        with self._state_lock:
            out = {}
            for channel in channels:
                reading = self._readings.get(channel)
                out[channel] = (reading if reading is not None and
                                (cycle is None or reading.cycle == cycle)
                                else None)
            return out

    @property
    def cycle(self):
        with self._state_lock:
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
        with self._state_lock:
            waiting = sum(count > 0 for count in self._group_failures.values())
        base = "scanning"
        if waiting:
            base += f" ({waiting} run frame(s) waiting)"
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
