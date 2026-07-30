"""Calibration run engine.

One RunEngine per calibration. Each owns its heat source outright and shares
the ADT286 through subscription. A ChannelRegistry guarantees no two runs
claim the same channel, which is what makes concurrent runs on one 286 safe.

Per set point the engine:
    1. sends the set point and confirms it by readback
    2. waits until the REFERENCE probe (read through the 286) is stable —
       peak-to-peak within a band over a rolling time window
    3. takes N samples of the reference and every DUT channel
    4. records mean / standard deviation / error and moves on
"""

import statistics
import threading
import time
from collections import deque

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "complete"
STATE_ABORTED = "aborted"
STATE_ERROR = "error"

PHASE_SETTING = "setting set point"
PHASE_STABILIZING = "waiting for stability"
PHASE_SOAKING = "soaking"
PHASE_SAMPLING = "sampling"
PHASE_IDLE = "idle"
PHASE_FINISHED = "finished"


class ChannelConflict(Exception):
    """Two runs tried to use the same ADT286 channel."""


class ChannelRegistry:
    """Exclusive ownership of ADT286 channels across concurrent runs."""

    def __init__(self):
        self._owner = {}
        self._lock = threading.Lock()

    def claim(self, run_id, channels):
        with self._lock:
            clashes = {c: o for c, o in self._owner.items()
                       if c in channels and o != run_id}
            if clashes:
                detail = ", ".join(f"{c} (used by {o})"
                                   for c, o in sorted(clashes.items()))
                raise ChannelConflict(
                    f"These channels are already in use: {detail}")
            for c in channels:
                self._owner[c] = run_id

    def release(self, run_id):
        with self._lock:
            for c in [c for c, o in self._owner.items() if o == run_id]:
                del self._owner[c]

    def owner(self, channel):
        with self._lock:
            return self._owner.get(channel)

    def in_use(self):
        with self._lock:
            return dict(self._owner)


def default_profile(name="New profile"):
    return {
        "name": name,
        "heat_source": "",          # key into the connected-source table
        "reference_channel": "",
        "dut_channels": [],
        "setpoints": [],
        # stability
        "stability_band": 0.02,     # peak-to-peak allowed, in display units
        "stability_window": 60.0,   # seconds the band must hold
        "max_wait": 2400.0,         # seconds before giving up on a point
        "require_near_setpoint": False,
        "setpoint_tolerance": 1.0,
        # sampling
        "sample_count": 10,
        "sample_interval": 5.0,
        "soak_seconds": 0.0,
        # behaviour
        "enable_output": True,
        "disable_at_end": True,
        "send_password": False,
        "on_timeout": "record",     # 'record' (flagged) or 'abort'
    }


MIN_STABILITY_SAMPLES = 3


def validate_profile(profile, heat_source=None, available_channels=None,
                     poll_interval=None):
    """Return a list of human-readable problems ([] means good to go)."""
    problems = []
    ref = (profile.get("reference_channel") or "").strip()
    duts = [c for c in profile.get("dut_channels", []) if c]
    sps = list(profile.get("setpoints", []))

    if not profile.get("name", "").strip():
        problems.append("Give the profile a name.")
    if not ref:
        problems.append("Select the channel the reference probe is on.")
    if not duts:
        problems.append("Select at least one device-under-test channel.")
    if ref and ref in duts:
        problems.append(
            f"{ref} is set as both the reference and a device under test.")
    if len(set(duts)) != len(duts):
        problems.append("The same DUT channel is listed more than once.")
    if not sps:
        problems.append("Add at least one set point.")
    if available_channels:
        for c in ([ref] if ref else []) + duts:
            if c not in available_channels:
                problems.append(f"{c} is not a channel on the connected 286.")
    if heat_source is not None:
        if not heat_source.is_open:
            problems.append(f"{heat_source.name} is not connected.")
        bad = heat_source.check_setpoints(sps)
        if bad:
            lo, hi = heat_source.range
            problems.append(
                f"Set points out of range for {heat_source.name} "
                f"({lo:g} to {hi:g} {heat_source.unit}): "
                + ", ".join(f"{v:g}" for v in bad))
    if profile.get("sample_count", 0) < 1:
        problems.append("Sample count must be at least 1.")
    if profile.get("stability_band", 0) <= 0:
        problems.append("Stability band must be greater than zero.")
    if profile.get("stability_window", 0) <= 0:
        problems.append("Stability window must be greater than zero.")
    if poll_interval:
        window = float(profile.get("stability_window") or 0)
        needed = MIN_STABILITY_SAMPLES * float(poll_interval)
        if 0 < window < needed:
            problems.append(
                f"The stability window ({window:g} s) is too short for the "
                f"286's scan rate (a reading about every {poll_interval:g} s). "
                f"Stability needs at least {MIN_STABILITY_SAMPLES} readings in "
                f"the window, so use {needed:g} s or more - otherwise every "
                "set point would time out even with a perfectly steady bath.")
    interval = float(profile.get("sample_interval") or 0)
    if poll_interval and 0 < interval < float(poll_interval):
        problems.append(
            f"Samples are requested every {interval:g} s but the 286 is only "
            f"scanned every {poll_interval:g} s, so samples would repeat. "
            f"Use {poll_interval:g} s or more.")
    return problems


class SetPointResult:
    """Everything measured at one set point."""

    def __init__(self, setpoint, unit):
        self.setpoint = setpoint
        self.unit = unit
        self.stable = False
        self.stabilize_seconds = 0.0
        self.note = ""
        self.started = time.time()
        self.finished = None
        self.samples = []              # [{'t', 'ref', 'duts': {ch: v}}]
        self.reference = {}            # mean/sd/n
        self.duts = {}                 # ch -> mean/sd/n/error

    # ----------------------------------------------------------- analysis --
    @staticmethod
    def _stats(values):
        vals = [v for v in values if v is not None]
        if not vals:
            return {"mean": None, "sd": None, "n": 0, "min": None, "max": None}
        return {
            "mean": statistics.fmean(vals),
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "n": len(vals),
            "min": min(vals),
            "max": max(vals),
        }

    def summarise(self, dut_channels):
        self.reference = self._stats([s["ref"] for s in self.samples])
        ref_mean = self.reference["mean"]
        for ch in dut_channels:
            st = self._stats([s["duts"].get(ch) for s in self.samples])
            st["error"] = (None if st["mean"] is None or ref_mean is None
                           else st["mean"] - ref_mean)
            self.duts[ch] = st
        self.finished = time.time()
        return self


class RunEngine:
    """Drives one calibration from start to finish on its own thread."""

    def __init__(self, run_id, profile, heat_source, adt, registry,
                 event_cb=None):
        self.run_id = run_id
        self.profile = dict(profile)
        self.heat_source = heat_source
        self.adt = adt
        self.registry = registry
        self.event_cb = event_cb or (lambda ev: None)

        self.state = STATE_IDLE
        self.phase = PHASE_IDLE
        self.results = []
        self.current_index = -1
        self.current_setpoint = None
        self.last_reference = None
        self.started_at = None
        self.finished_at = None
        self.error = ""

        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- events --
    def _emit(self, kind, **kw):
        ev = {"run_id": self.run_id, "name": self.profile.get("name", ""),
              "kind": kind, "t": time.time()}
        ev.update(kw)
        try:
            self.event_cb(ev)
        except Exception:
            pass

    def _log(self, tag, message):
        self._emit("log", tag=tag, message=message)

    def _set_phase(self, phase):
        self.phase = phase
        self._emit("phase", phase=phase, index=self.current_index,
                   setpoint=self.current_setpoint)

    # -------------------------------------------------------- lifecycle ----
    @property
    def channels(self):
        ref = self.profile.get("reference_channel")
        return ([ref] if ref else []) + list(self.profile.get("dut_channels", []))

    @property
    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_active:
            raise RuntimeError("This run is already going.")
        problems = validate_profile(self.profile, self.heat_source,
                                    self.adt.channels or None,
                                    getattr(self.adt, "poll_interval", None))
        if problems:
            raise ValueError("; ".join(problems))
        self.registry.claim(self.run_id, self.channels)
        try:
            self.adt.subscribe(self.run_id, self.channels)
        except Exception:
            self.registry.release(self.run_id)
            raise
        self.results = []
        self.error = ""
        self._stop.clear()
        self.state = STATE_RUNNING
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._emit("state", state=self.state)

    def stop(self, reason="stopped by the operator"):
        self._stop.set()
        self.error = self.error or reason

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # --------------------------------------------------------- internals ---
    def _sleep(self, seconds):
        """Interruptible wait. Returns False if a stop was requested."""
        return not self._stop.wait(seconds)

    def _fresh_reference(self, last_cycle):
        """Wait for a scan cycle newer than last_cycle. -> (cycle, reading)."""
        deadline = time.time() + 15.0
        while not self._stop.is_set() and time.time() < deadline:
            r = self.adt.latest(self.profile["reference_channel"])
            if r is not None and r.cycle > last_cycle and r.temperature is not None:
                return r.cycle, r
            self._stop.wait(0.2)
        return last_cycle, None

    def _stabilize(self, setpoint):
        """Hold until the reference is flat within the band over the window."""
        band = float(self.profile["stability_band"])
        window = float(self.profile["stability_window"])
        max_wait = float(self.profile["max_wait"])
        near = bool(self.profile.get("require_near_setpoint"))
        sp_tol = float(self.profile.get("setpoint_tolerance", 1.0))

        history = deque()          # (timestamp, value) for the trailing window
        start = time.time()
        watching_since = None      # first reading seen at this set point
        last_cycle = -1
        self._set_phase(PHASE_STABILIZING)
        while not self._stop.is_set():
            elapsed = time.time() - start
            if elapsed > max_wait:
                if len(history) < 3:
                    return (False, elapsed,
                            "stability timeout - only "
                            f"{len(history)} reading(s) fell inside the "
                            f"{window:g}s window, so it could never be "
                            "judged; lengthen the window or scan faster")
                values = [v for _, v in history]
                return (False, elapsed,
                        f"stability timeout - last span "
                        f"{max(values) - min(values):.4f} vs band {band:g}")
            last_cycle, reading = self._fresh_reference(last_cycle)
            if reading is None:
                if self._stop.is_set():
                    break
                self._log("WARN", "No reference data from the 286 — is the "
                                  "channel enabled and the probe connected?")
                if not self._sleep(1.0):
                    break
                continue
            now = reading.timestamp or time.time()
            if watching_since is None:
                watching_since = now
            self.last_reference = reading.temperature
            history.append((now, reading.temperature))
            while history and now - history[0][0] > window:
                history.popleft()
            self._emit("reference", value=reading.temperature,
                       setpoint=setpoint, elapsed=elapsed,
                       span=(max(v for _, v in history)
                             - min(v for _, v in history)) if history else None)
            # "Stable" means: we have watched this set point for at least the
            # window, AND the most recent window of readings is flat within
            # the band. Judging coverage by how long we have been watching --
            # rather than by the age of the oldest kept sample -- keeps the
            # test independent of how the sample times happen to line up.
            covered = (now - watching_since) >= window
            if covered and len(history) >= 3:
                values = [v for _, v in history]
                span = max(values) - min(values)
                if span <= band:
                    if near:
                        offset = abs(statistics.fmean(values) - setpoint)
                        if offset > sp_tol:
                            self._log("INFO",
                                      f"Flat at {statistics.fmean(values):.3f} "
                                      f"but {offset:.3f} from the set point; "
                                      "still waiting.")
                            if not self._sleep(1.0):
                                break
                            continue
                    return True, time.time() - start, ""
            if not self._sleep(0.2):
                break
        return False, time.time() - start, "stopped"

    def _take_samples(self, result):
        count = int(self.profile["sample_count"])
        interval = float(self.profile["sample_interval"])
        duts = list(self.profile["dut_channels"])
        ref_ch = self.profile["reference_channel"]
        self._set_phase(PHASE_SAMPLING)
        last_cycle = -1
        for i in range(count):
            if self._stop.is_set():
                return False
            last_cycle, reading = self._fresh_reference(last_cycle)
            if reading is None:
                self._log("WARN", f"Sample {i + 1}: no fresh reference data.")
                if not self._sleep(interval):
                    return False
                continue
            snap = self.adt.snapshot([ref_ch] + duts)
            sample = {
                "t": time.time(),
                "ref": (snap[ref_ch].temperature
                        if snap.get(ref_ch) else None),
                "duts": {c: (snap[c].temperature if snap.get(c) else None)
                         for c in duts},
            }
            result.samples.append(sample)
            self._emit("sample", index=i + 1, of=count, sample=sample,
                       setpoint=result.setpoint)
            if i < count - 1 and not self._sleep(interval):
                return False
        return True

    def _run(self):
        hs = self.heat_source
        unit = hs.unit
        try:
            if self.profile.get("enable_output"):
                if not hs.enable_output():
                    self._log("WARN", f"{hs.name} has no remote output-enable "
                                      "command — switch heating on at the "
                                      "front panel or the run will never "
                                      "reach its set points.")
            for idx, sp in enumerate(self.profile["setpoints"]):
                if self._stop.is_set():
                    break
                self.current_index = idx
                self.current_setpoint = sp
                result = SetPointResult(sp, unit)

                self._set_phase(PHASE_SETTING)
                hs.set_setpoint(sp, send_password=self.profile.get(
                    "send_password", False))
                ok, rb = hs.confirm_setpoint(sp)
                if ok is False:
                    self._log("WARN", f"Set point readback is {rb}, expected "
                                      f"{sp:g}. If it did not change, the set "
                                      "point may be locked on the instrument.")
                elif ok:
                    self._log("PASS", f"Set point {sp:g} {unit} confirmed.")

                stable, secs, note = self._stabilize(sp)
                result.stable = stable
                result.stabilize_seconds = secs
                result.note = note
                if self._stop.is_set():
                    break
                if not stable:
                    if self.profile.get("on_timeout") == "abort":
                        self.error = (f"Set point {sp:g} {unit} did not "
                                      f"stabilise within "
                                      f"{self.profile['max_wait']:.0f}s.")
                        self.state = STATE_ERROR
                        self._log("FAIL", self.error)
                        break
                    self._log("WARN", f"Set point {sp:g} {unit} never met the "
                                      "stability band; sampling anyway and "
                                      "flagging the result.")
                else:
                    self._log("PASS", f"Stable at {sp:g} {unit} after "
                                      f"{secs:.0f}s.")

                soak = float(self.profile.get("soak_seconds") or 0)
                if soak > 0:
                    self._set_phase(PHASE_SOAKING)
                    if not self._sleep(soak):
                        break

                if not self._take_samples(result):
                    break
                result.summarise(self.profile["dut_channels"])
                self.results.append(result)
                self._emit("result", result=result, index=idx)
            else:
                if not self._stop.is_set() and self.state == STATE_RUNNING:
                    self.state = STATE_DONE
        except Exception as e:
            self.state = STATE_ERROR
            self.error = str(e)
            self._log("FAIL", f"Run stopped: {e}")
        finally:
            if self.state == STATE_RUNNING:
                self.state = (STATE_ABORTED if self._stop.is_set()
                              else STATE_DONE)
            try:
                if self.profile.get("disable_at_end"):
                    self.heat_source.disable_output()
            except Exception as e:
                self._log("WARN", f"Could not switch the output off: {e}")
            try:
                self.adt.unsubscribe(self.run_id)
            finally:
                self.registry.release(self.run_id)
            self.finished_at = time.time()
            self.current_setpoint = None
            self._set_phase(PHASE_FINISHED)
            self._emit("state", state=self.state)
            self._log("INFO", f"Run {self.state}. "
                              f"{len(self.results)} set point(s) recorded.")
