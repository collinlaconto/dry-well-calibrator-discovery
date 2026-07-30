"""Transports: serial (USB/RS-232), TCP (Ethernet/Wi-Fi), and Bluetooth.

A Link owns the protocol details that are the same everywhere - terminator,
echo stripping, assembling a reply - while a Transport moves the bytes. So an
instrument's command set is independent of how it is plugged in: the same
profile works over USB today and Wi-Fi tomorrow.

Connection specs are plain dicts so they can live in the JSON profile files:

    {"kind": "serial",    "port": "COM5", "baud": 9600}
    {"kind": "tcp",       "host": "192.168.1.50", "port": 5025}
    {"kind": "bluetooth", "address": "AA:BB:CC:DD:EE:FF", "channel": 1}
"""

import re
import socket
import time

try:
    import serial
    from serial.tools import list_ports
    SERIAL_OK = True
    SERIAL_ERROR = ""
except Exception as _e:                                   # pragma: no cover
    SERIAL_OK = False
    SERIAL_ERROR = str(_e)

# Ports worth trying when looking for a networked instrument. 8000 is
# Additel's default and goes first; 5025 is the IANA "scpi-raw" port used by
# most other lab gear; the rest are common alternatives.
CANDIDATE_TCP_PORTS = (8000, 5025, 5000, 8080, 8888, 2000, 23, 1024, 10001)

DEFAULT_TCP_PORT = 8000


def available_ports():
    """[(device, description)] for every serial port on the machine."""
    if not SERIAL_OK:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


def normalize_target(target, default_baud="9600"):
    """Canonical connection target, tolerant of older/looser shapes.

        {"kind": "serial",    "port": "COM5", "baud": "9600"}
        {"kind": "tcp",       "host": "192.168.1.50", "tcp_port": 5025}
        {"kind": "bluetooth", "port": "COM7"}            paired SPP port
        {"kind": "bluetooth", "address": "AA:BB:...", "channel": 1}
    """
    default_baud = str(default_baud or "9600")
    if not target:
        return {"kind": "serial", "port": "", "baud": default_baud}
    if isinstance(target, str):
        return {"kind": "serial", "port": target, "baud": default_baud}
    t = dict(target)
    kind = t.get("kind", "serial")
    if kind == "tcp":
        raw = t.get("tcp_port", t.get("port", DEFAULT_TCP_PORT))
        try:
            port = int(str(raw).strip() or DEFAULT_TCP_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_TCP_PORT
        return {"kind": "tcp", "host": str(t.get("host", "")).strip(),
                "tcp_port": port}
    if kind == "bluetooth" and t.get("address"):
        return {"kind": "bluetooth", "address": str(t["address"]).strip(),
                "channel": int(t.get("channel", 1) or 1)}
    return {"kind": kind, "port": str(t.get("port", "")).strip(),
            "baud": str(t.get("baud") or default_baud)}


def target_is_set(target):
    """True when a target has enough detail to attempt a connection."""
    t = normalize_target(target)
    if t["kind"] == "tcp":
        return bool(t["host"])
    if t["kind"] == "bluetooth" and "address" in t:
        return bool(t["address"])
    return bool(t["port"])


def describe_target(target):
    """One-line human description of a connection target."""
    if not target:
        return "(not set)"
    t = normalize_target(target)
    if t["kind"] == "tcp":
        return f"{t['host'] or '?'}:{t['tcp_port']}"
    if t["kind"] == "bluetooth" and "address" in t:
        return f"Bluetooth {t['address']} ch{t['channel']}"
    label = "Bluetooth port" if t["kind"] == "bluetooth" else "serial"
    return f"{t.get('port') or '?'} ({label} @ {t.get('baud', '9600')})"


# Older name kept so existing calls keep working.
describe_spec = describe_target


# --------------------------------------------------------------- transports --
class Transport:
    """Moves bytes. Subclasses implement the four underscore methods."""

    def __init__(self, spec):
        self.spec = dict(spec)

    @property
    def is_open(self):
        raise NotImplementedError

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def _write(self, data):
        raise NotImplementedError

    def _read(self, limit=4096):
        """Return whatever bytes are available now; b'' if none."""
        raise NotImplementedError

    def _reset_input(self):
        pass

    def describe(self):
        return describe_spec(self.spec)


class SerialTransport(Transport):
    """USB virtual COM port or RS-232. Also covers Bluetooth SPP on Windows,
    where a paired device shows up as an outgoing COM port."""

    def __init__(self, spec):
        super().__init__(spec)
        self.ser = None

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self):
        if not SERIAL_OK:
            raise RuntimeError(
                "pyserial is not installed - run: pip install pyserial")
        self.close()
        self.ser = serial.Serial(
            port=self.spec["port"],
            baudrate=int(self.spec.get("baud", 9600)),
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=0.15, write_timeout=2.0)

    def close(self):
        if self.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _write(self, data):
        self.ser.write(data)
        self.ser.flush()

    def _read(self, limit=4096):
        n = self.ser.in_waiting
        return self.ser.read(min(n, limit) if n else 1)

    def _reset_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass


class TcpTransport(Transport):
    """Raw SCPI over TCP - Ethernet or Wi-Fi."""

    def __init__(self, spec):
        super().__init__(spec)
        self.sock = None
        self.connect_timeout = float(spec.get("connect_timeout", 5.0))

    @property
    def is_open(self):
        return self.sock is not None

    def open(self):
        self.close()
        host = self.spec["host"]
        port = int(self.spec.get("tcp_port",
                                 self.spec.get("port", DEFAULT_TCP_PORT)))
        sock = socket.create_connection((host, port),
                                        timeout=self.connect_timeout)
        sock.settimeout(0.15)          # short read timeout; Link does the wait
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = sock

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _write(self, data):
        self.sock.sendall(data)

    def _read(self, limit=4096):
        try:
            return self.sock.recv(limit)
        except socket.timeout:
            return b""
        except OSError:
            return b""

    def _reset_input(self):
        """Drain anything already queued so a reply can't be mistaken."""
        end = time.time() + 0.05
        while time.time() < end:
            if not self._read():
                break


class BluetoothTransport(Transport):
    """Bluetooth RFCOMM (Serial Port Profile).

    Works natively on Linux via AF_BLUETOOTH. On Windows and macOS, Python
    has no RFCOMM socket, but a paired SPP device is exposed as a virtual
    COM port - connect to that with the serial transport instead. This class
    says so plainly rather than failing obscurely.
    """

    def __init__(self, spec):
        super().__init__(spec)
        self.sock = None

    @property
    def is_open(self):
        return self.sock is not None

    @staticmethod
    def supported():
        return (hasattr(socket, "AF_BLUETOOTH")
                and hasattr(socket, "BTPROTO_RFCOMM"))

    def open(self):
        self.close()
        if not self.supported():
            raise RuntimeError(
                "This platform has no direct Bluetooth socket support.\n\n"
                "Pair the instrument in the operating system's Bluetooth "
                "settings. A paired Serial Port Profile device appears as an "
                "outgoing COM port - connect to that COM port with the "
                "Serial option instead.")
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                             socket.BTPROTO_RFCOMM)
        sock.settimeout(float(self.spec.get("connect_timeout", 10.0)))
        sock.connect((self.spec["address"], int(self.spec.get("channel", 1))))
        sock.settimeout(0.15)
        self.sock = sock

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _write(self, data):
        self.sock.sendall(data)

    def _read(self, limit=4096):
        try:
            return self.sock.recv(limit)
        except socket.timeout:
            return b""
        except OSError:
            return b""


TRANSPORTS = {"serial": SerialTransport, "tcp": TcpTransport,
              "bluetooth": BluetoothTransport}


def make_transport(target):
    """Build the right transport for a connection target.

    A Bluetooth target given as a paired COM port uses the serial transport,
    because that is exactly what pairing produces on Windows. Only a target
    carrying a Bluetooth address needs a real RFCOMM socket.
    """
    t = normalize_target(target)
    kind = t["kind"]
    if kind == "tcp":
        return TcpTransport(t)
    if kind == "bluetooth" and "address" in t:
        return BluetoothTransport(t)
    if kind not in ("serial", "bluetooth"):
        raise ValueError(f"Unknown connection type '{kind}'. Expected one "
                         f"of: serial, bluetooth, tcp.")
    return SerialTransport(t)


# --------------------------------------------------------------------- link --
class Link:
    """Command/reply protocol on top of any transport."""

    def __init__(self, spec=None, terminator="\r\n", reply_timeout=1.5,
                 logger=None):
        self.transport = make_transport(spec) if spec else None
        self.terminator = terminator
        self.reply_timeout = reply_timeout
        self.log = logger or (lambda tag, msg: None)

    # -------------------------------------------------------------- session --
    @property
    def is_open(self):
        return self.transport is not None and self.transport.is_open

    @property
    def spec(self):
        return dict(self.transport.spec) if self.transport else {}

    def describe(self):
        return self.transport.describe() if self.transport else "(not set)"

    def open(self, spec=None, terminator=None, reply_timeout=None):
        if spec is not None:
            self.close()
            self.transport = make_transport(spec)
        if terminator:
            self.terminator = terminator
        if reply_timeout:
            self.reply_timeout = reply_timeout
        if self.transport is None:
            raise RuntimeError("No connection details were given.")
        self.transport.open()
        self.log("INFO", f"Opened {self.transport.describe()}")

    def close(self):
        if self.transport is not None:
            self.transport.close()

    # -------------------------------------------------------------- traffic --
    def write(self, cmd):
        """Send a command that returns nothing."""
        if not self.is_open:
            raise RuntimeError("Connection is not open")
        self.transport._reset_input()
        self.transport._write((cmd + self.terminator).encode("ascii",
                                                             "replace"))
        self.log("TX", cmd)

    def query(self, cmd):
        """Send a query; return the reply with any command echo removed."""
        if not self.is_open:
            raise RuntimeError("Connection is not open")
        self.transport._reset_input()
        self.transport._write((cmd + self.terminator).encode("ascii",
                                                             "replace"))
        self.log("TX", cmd)
        deadline = time.time() + self.reply_timeout
        buf = b""
        while time.time() < deadline:
            chunk = self.transport._read()
            if chunk:
                buf += chunk
                if buf.endswith(b"\n") or buf.endswith(b"\r"):
                    time.sleep(0.03)
                    extra = self.transport._read()
                    if not extra:
                        break
                    buf += extra
        text = buf.decode("ascii", "replace")
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
        if lines and lines[0].strip().upper() == cmd.strip().upper():
            lines = lines[1:]                    # strip echo
        reply = lines[0] if lines else ""
        self.log("RX", reply or "<no reply>")
        return reply


def make_link(target, terminator="\r\n", reply_timeout=1.5, logger=None):
    """A Link bound to a connection target, whatever the transport."""
    return Link(target, terminator=terminator, reply_timeout=reply_timeout,
                logger=logger)


# Backwards-compatible alias: earlier code called this SerialLink.
SerialLink = Link


# ------------------------------------------------------------------ finding --
def probe_tcp(host, port, timeout=1.0, identify="*IDN?", terminator="\r\n"):
    """Try one host:port. Returns the identity string, '' if it answered
    but not to *IDN?, or None if nothing is listening."""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        sock.sendall((identify + terminator).encode("ascii", "replace"))
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n") or buf.endswith(b"\r"):
                break
        text = buf.decode("ascii", "replace").strip()
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
        if lines and lines[0].upper() == identify.upper():
            lines = lines[1:]
        return lines[0] if lines else ""
    finally:
        try:
            sock.close()
        except Exception:
            pass


def find_tcp_port(host, ports=CANDIDATE_TCP_PORTS, timeout=1.0,
                  progress=None):
    """Ask each likely port for its identity; return [(port, idn)].

    Only ports that actually answered *IDN?* are returned, so a stray open
    port that says nothing cannot be mistaken for the instrument.
    """
    found = []
    for port in ports:
        if progress:
            try:
                progress(port)
            except Exception:
                pass
        idn = probe_tcp(host, port, timeout=timeout)
        if idn:
            found.append((port, idn))
    return found


def scan_tcp_ports(host, ports=CANDIDATE_TCP_PORTS, timeout=1.0):
    """Find which port an instrument is listening on.

    Returns [(port, identity)] - identity may be '' if the port is open but
    did not answer *IDN?. Ordered with identified instruments first.
    """
    found = []
    for port in ports:
        idn = probe_tcp(host, port, timeout=timeout)
        if idn is not None:
            found.append((port, idn))
    found.sort(key=lambda pair: (pair[1] == "", pair[0]))
    return found
