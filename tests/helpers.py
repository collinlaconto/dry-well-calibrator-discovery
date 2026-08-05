"""Small device doubles shared by the core integrity tests."""

from types import SimpleNamespace

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()


class FakeHeatSource:
    def __init__(self, *, unit="°C", reported_unit="°C", confirm=(True, 0.0)):
        self.name = "Test Well"
        self.unit = unit
        self.reported_unit = reported_unit
        self.range = (-100.0, 200.0)
        self.is_open = True
        self.idn = "WELL,MODEL,SERIAL,1.0"
        self.connection = "COM-WELL"
        self.profile = {
            "port": "COM-WELL",
            "sp_write": "SET {value}",
            "sp_read": "SET?",
        }
        self.last_setpoint_command = ""
        self.last_setpoint_readback_raw = ""
        self.last_setpoint_readback_unit_raw = reported_unit
        self.last_setpoint_readback_unit = reported_unit
        self.confirmation = confirm
        self.setpoints = []
        self.disabled = False

    def check_setpoints(self, setpoints):
        lo, hi = self.range
        return [value for value in setpoints if not lo <= value <= hi]

    def set_setpoint(self, value, send_password=False):
        self.setpoints.append((value, send_password))
        self.last_setpoint_command = f"SET {value!r}"
        return True

    def confirm_setpoint(self, value):
        readback = self.confirmation[1]
        self.last_setpoint_readback_raw = (
            "" if readback is None else repr(float(readback)))
        self.last_setpoint_readback_unit = self.reported_unit
        self.last_setpoint_readback_unit_raw = self.reported_unit
        return self.confirmation

    def enable_output(self):
        return True

    def disable_output(self):
        self.disabled = True
        return True


class FakeRegistry:
    def __init__(self):
        self.claimed = []
        self.released = []

    def claim(self, run_id, channels):
        self.claimed.append((run_id, tuple(channels)))

    def release(self, run_id):
        self.released.append(run_id)


class MinimalAdt:
    def __init__(self, *, unit="°C"):
        self.unit = unit
        self.channels = ["REF", "DUT"]
        self.channel_info = {
            "REF": {"type": "SPRT", "serial": "REF-SERIAL"},
            "DUT": {"type": "PRT", "serial": "DUT-SERIAL"},
        }
        self.poll_interval = 1.0
        self.idn = "ADDITEL,ADT286,SERIAL,1.0"
        self.link = SimpleNamespace(describe=lambda: "COM-ADT")
        self.is_open = True
        self.cycle = 0
        self.unsubscribed = []

    def subscribe(self, owner, channels):
        return None

    def unsubscribe(self, owner):
        self.unsubscribed.append(owner)
