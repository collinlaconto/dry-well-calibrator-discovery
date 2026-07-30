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
    SCAN:DATA:Last?                most recent scan data for those channels
    SCAN:STOP                      stop scanning
    UNIT:TEMPerature?              system temperature unit
    SYSTem:ERRor?                  error queue

Reply format for SCAN:DATA:Last? (per channel, groups split by ';'):
    name, elecUnitId, n, <2n electrical values>, indUnitId, m, <m values>[, CJC...]
e.g. "REF1,1281,1,28.258167,28.258167,1001,1,33.512077;"
"""

import threading
import time

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


class Reading:
    """One channel's most recent scan sample."""

    __slots__ = ("channel", "temperature", "unit", "electrical",
                 "electrical_unit", "cycle", "timestamp")

    def __init__(self, channel, temperature=None, unit=None, electrical=None,
                 electrical_unit=None, cycle=0, timestamp=0.0):
        self.channel = channel
        self.temperature = temperature
        self.unit = unit
        self.electrical = electrical
        self.electrical_unit = electrical_unit
        self.cycle = cycle
        self.timestamp = timestamp

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
    for group in payload.strip().strip('"').split(";"):
        group = group.strip()
        if not group:
            continue
        f = [x.strip() for x in group.split(",")]
        if len(f) < 3 or not f[0]:
            continue
        name = f[0]
        try:
            elec_unit = int(f[1])
            n_elec = int(f[2])
        except ValueError:
            continue
        elec = None
        if len(f) > 3 and f[3]:
            try:
                elec = float(f[3])
            except ValueError:
                elec = None
        temp = t_unit = None
        idx = 3 + 2 * n_elec              # raw + filtered per electrical point
        if len(f) > idx + 1:
            try:
                unit_id = int(f[idx])
                m = int(f[idx + 1])
                if unit_id in TEMP_UNIT_IDS and m >= 1 and len(f) > idx + 2:
                    temp = float(f[idx + 2])
                    t_unit = TEMP_UNIT_IDS[unit_id]
            except (ValueError, IndexError):
                pass
        out[name] = {"temperature": temp, "unit": t_unit, "electrical": elec,
                     "electrical_unit": ELEC_UNIT_IDS.get(elec_unit,
                                                          str(elec_unit))}
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
    return {"name": f[0], "enabled": f[1] == "1", "label": f[2],
            "type": ctype, "type_name": CHANNEL_TYPES.get(ctype, f"type {ctype}"),
            "sensor": sensor}


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

    # ------------------------------------------------------------ session --
    @property
    def is_open(self):
        return self.link is not None and self.link.is_open

    def connect(self, target, baud=9600):
        """Connect over USB/serial or the network (Ethernet / Wi-Fi)."""
        t = normalize_target(target, str(baud))
        if not target_is_set(t):
            raise RuntimeError("No port or address given for the ADT286.")
        with self.lock:
            self.link = make_link(t, terminator="\r\n", reply_timeout=2.0)
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
        self._subs.clear()
        self._readings.clear()
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
            self._restart_scan()

    def unsubscribe(self, owner):
        with self.lock:
            self._subs.pop(owner, None)
            self._restart_scan()

    def subscribed_channels(self):
        seen = []
        for chans in self._subs.values():
            for c in chans:
                if c not in seen:
                    seen.append(c)
        return seen

    def _restart_scan(self):
        """Reconfigure the single shared scan for the union of subscribers."""
        chans = self.subscribed_channels()
        if self.link is None or not self.link.is_open:
            return
        if not chans:
            try:
                self.link.write("SCAN:STOP")
                self.log("INFO", "No subscribers left; scanning stopped.")
            except Exception as e:
                self.log("WARN", f"SCAN:STOP failed: {e}")
            return
        cmd = f'SCAN:MULT:STARt {self.scan_rate},"{",".join(chans)}"'
        try:
            self.link.write(cmd)
            self.log("INFO", f"Scanning {len(chans)} channel(s): "
                             f"{', '.join(chans)}")
        except Exception as e:
            self.log("FAIL", f"Could not start scan: {e}")

    # ---------------------------------------------------------- polling ----
    def _start_poller(self):
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

    def poll_once(self):
        """One scan read. Returns the number of channels updated."""
        if (self.link is None or not self.link.is_open
                or not self.subscribed_channels()):
            return 0
        with self.lock:
            try:
                payload = self.link.query("SCAN:DATA:Last?")
            except Exception as e:
                self.last_error = str(e)
                return 0
            data = parse_scan_data(payload)
            if not data:
                return 0
            self._cycle += 1
            now = time.time()
            for name, vals in data.items():
                self._readings[name] = Reading(
                    name, vals["temperature"], vals["unit"],
                    vals["electrical"], vals["electrical_unit"],
                    self._cycle, now)
            return len(data)

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            try:
                self.poll_once()
            except Exception as e:                        # pragma: no cover
                self.last_error = str(e)
            self._poll_stop.wait(self.poll_interval)

    # ---------------------------------------------------------- readings ---
    def latest(self, channel):
        with self.lock:
            return self._readings.get(channel)

    def snapshot(self, channels):
        """Readings for several channels from the same scan cycle if possible."""
        with self.lock:
            return {c: self._readings.get(c) for c in channels}

    @property
    def cycle(self):
        with self.lock:
            return self._cycle

    def system_error(self):
        with self.lock:
            try:
                return self.link.query("SYSTem:ERRor?")
            except Exception as e:
                return str(e)
