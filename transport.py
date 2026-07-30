"""Serial transport shared by heat sources and the ADT286.

Every instrument gets its own SerialLink on its own port, so heat sources
never contend with each other. The ADT286's link is additionally guarded by
a lock inside Adt286 because several runs share it.
"""

import re
import threading
import time

try:
    import serial
    from serial.tools import list_ports
    SERIAL_OK = True
    SERIAL_ERROR = ""
except Exception as _e:                                   # pragma: no cover
    SERIAL_OK = False
    SERIAL_ERROR = str(_e)


def available_ports():
    """[(device, description)] for every serial port on the machine."""
    if not SERIAL_OK:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


class SerialLink:
    """One serial connection. Not thread-safe on its own; callers lock."""

    def __init__(self, terminator="\r\n", reply_timeout=1.5, logger=None):
        self.ser = None
        self.terminator = terminator
        self.reply_timeout = reply_timeout
        self.log = logger or (lambda tag, msg: None)
        self.port = ""
        self.baud = ""

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port, baud=9600, terminator=None, reply_timeout=None):
        self.close()
        if terminator:
            self.terminator = terminator
        if reply_timeout:
            self.reply_timeout = reply_timeout
        self.ser = serial.Serial(
            port=port, baudrate=int(baud), bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=0.15, write_timeout=2.0)
        self.port, self.baud = port, str(baud)
        self.log("INFO", f"Opened {port} @ {baud}")

    def close(self):
        if self.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def write(self, cmd):
        """Send a command that returns nothing."""
        if not self.is_open:
            raise RuntimeError("Port is not open")
        self.ser.reset_input_buffer()
        self.ser.write((cmd + self.terminator).encode("ascii", "replace"))
        self.ser.flush()
        self.log("TX", cmd)

    def query(self, cmd):
        """Send a query and return the reply with any command echo removed."""
        if not self.is_open:
            raise RuntimeError("Port is not open")
        self.ser.reset_input_buffer()
        self.ser.write((cmd + self.terminator).encode("ascii", "replace"))
        self.ser.flush()
        self.log("TX", cmd)
        deadline = time.time() + self.reply_timeout
        buf = b""
        while time.time() < deadline:
            n = self.ser.in_waiting
            chunk = self.ser.read(n if n else 1)
            if chunk:
                buf += chunk
                if buf.endswith(b"\n") or buf.endswith(b"\r"):
                    time.sleep(0.05)
                    if not self.ser.in_waiting:
                        break
        text = buf.decode("ascii", "replace")
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
        if lines and lines[0].strip().upper() == cmd.strip().upper():
            lines = lines[1:]                    # strip echo
        reply = lines[0] if lines else ""
        self.log("RX", reply or "<no reply>")
        return reply
