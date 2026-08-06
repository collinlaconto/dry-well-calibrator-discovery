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

import copy
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Mapping
from types import MappingProxyType

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


class _SourceUnitVerificationError(RuntimeError):
    """A live source-unit check failed, with its current reply evidence."""

    def __init__(self, message, *, reported="", raw="", query_succeeded=False):
        super().__init__(message)
        self.reported = str(reported or "")
        self.raw = str(raw or "")
        self.query_succeeded = bool(query_succeeded)


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
        # tolerance: how far a device may sit from the reference and pass
        "tolerance_mode": "single",   # 'single' across the range, or 'per_point'
        "tolerance": 0.05,
        "tolerances": [],             # one per set point when per_point
        # stability
        "stability_band": 0.02,     # peak-to-peak allowed, in display units
        "stability_window": 60.0,   # seconds the band must hold
        "max_wait": 2400.0,         # seconds before giving up on a point
        "require_near_setpoint": True,
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
MAX_SAMPLE_COUNT = 10000


def _deep_freeze(value):
    """Return an immutable, recursively detached evidence snapshot."""
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _deep_freeze(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalise_unit(value):
    unit = str(value or "").strip().upper().replace("DEG", "").replace("°", "")
    aliases = {"CELSIUS": "C", "FAHRENHEIT": "F", "KELVIN": "K",
               "RANKINE": "R"}
    return aliases.get(unit, unit)


def tolerance_at(profile, index):
    """The tolerance that applies at set point `index`."""
    if (profile.get("tolerance_mode") or "single") == "per_point":
        values = profile.get("tolerances") or []
        if index < len(values):
            value = _finite_number(values[index])
            return value if value is not None and value > 0 else None
        return None
    value = _finite_number(profile.get("tolerance"))
    return value if value is not None and value > 0 else None


def validate_profile(profile, heat_source=None, available_channels=None,
                     poll_interval=None, readout_unit=None):
    """Return a list of human-readable problems ([] means good to go)."""
    problems = []
    ref = str(profile.get("reference_channel") or "").strip()
    raw_duts = profile.get("dut_channels", [])
    if not isinstance(raw_duts, (list, tuple)):
        problems.append("DUT channels must be a list of channel names.")
        raw_duts = []
    duts = [str(c).strip() for c in raw_duts if str(c).strip()]
    raw_setpoints = profile.get("setpoints", [])
    if not isinstance(raw_setpoints, (list, tuple)):
        problems.append("Set points must be a list of numbers.")
        sps = []
    else:
        sps = list(raw_setpoints)

    if not str(profile.get("name") or "").strip():
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
    bad_setpoints = [value for value in sps if _finite_number(value) is None]
    if bad_setpoints:
        problems.append("Every set point must be a finite number.")
    finite_sps = [_finite_number(value) for value in sps
                  if _finite_number(value) is not None]
    if available_channels is not None:
        for c in ([ref] if ref else []) + duts:
            if c not in available_channels:
                problems.append(f"{c} is not a channel on the connected 286.")
    if not str(profile.get("heat_source") or "").strip():
        problems.append("Select a heat source.")
    elif heat_source is None:
        problems.append("The selected heat source is not connected.")
    else:
        if not heat_source.is_open:
            problems.append(f"{heat_source.name} is not connected.")
        source_profile = getattr(heat_source, "profile", {})
        if not str(source_profile.get("sp_write") or "").strip():
            problems.append(
                f"{heat_source.name} has no set-point write command. Run "
                "command discovery before calibrating.")
        if not str(source_profile.get("sp_read") or "").strip():
            problems.append(
                f"{heat_source.name} has no set-point readback command. A "
                "calibration cannot start without device confirmation.")
        bad = heat_source.check_setpoints(finite_sps)
        if bad:
            lo, hi = heat_source.range
            problems.append(
                f"Set points out of range for {heat_source.name} "
                f"({lo:g} to {hi:g} {heat_source.unit}): "
                + ", ".join(f"{v:g}" for v in bad))
        configured = _normalise_unit(heat_source.unit)
        reported = _normalise_unit(getattr(heat_source, "reported_unit", ""))
        measured = _normalise_unit(readout_unit)
        if not reported:
            problems.append(
                f"{heat_source.name} did not report its current temperature "
                "unit. Configure a working unit query (or a set-point "
                "readback that includes the unit) before calibrating.")
        if reported and configured and reported != configured:
            problems.append(
                f"{heat_source.name} reports {getattr(heat_source, 'reported_unit', '')} "
                f"but its configured range is in {heat_source.unit}. Correct the "
                "instrument or profile before sending a set point.")
        source_unit = reported
        if readout_unit is not None and not measured:
            problems.append(
                "The ADT286 did not report a temperature unit. Confirm its "
                "unit on the instrument and reconnect before calibrating.")
        if measured and source_unit and measured != source_unit:
            problems.append(
                f"Unit mismatch: the ADT286 reports {readout_unit}, while "
                f"{heat_source.name} uses "
                f"{getattr(heat_source, 'reported_unit', '') or heat_source.unit}. "
                "The suite does not convert calibration readings; set both "
                "instruments to the same unit.")
    mode = profile.get("tolerance_mode") or "single"
    if mode == "per_point":
        values = profile.get("tolerances") or []
        if not isinstance(values, (list, tuple)):
            problems.append("Per-point tolerances must be a list of numbers.")
            values = []
        if len(values) != len(sps):
            problems.append(
                f"Per-point tolerances: {len(values)} given for "
                f"{len(sps)} set point(s). Give one for each, or switch to a "
                "single tolerance for the whole range.")
        bad = [v for v in values if not _positive(v)]
        if bad:
            problems.append(
                "Every tolerance must be a number greater than zero.")
    elif not _positive(profile.get("tolerance")):
        problems.append("Tolerance must be a number greater than zero.")
    count = _finite_number(profile.get("sample_count"))
    if (count is None or count < 1 or int(count) != count or
            count > MAX_SAMPLE_COUNT):
        problems.append(
            f"Sample count must be a whole number from 1 to {MAX_SAMPLE_COUNT:,}.")
    band = _finite_number(profile.get("stability_band"))
    if band is None or band <= 0:
        problems.append("Stability band must be a finite number greater than zero.")
    window = _finite_number(profile.get("stability_window"))
    if window is None or window <= 0:
        problems.append("Stability window must be a finite number greater than zero.")
    max_wait = _finite_number(profile.get("max_wait"))
    if max_wait is None or max_wait <= 0:
        problems.append("Maximum wait must be a finite number greater than zero.")
    interval = _finite_number(profile.get("sample_interval"))
    if interval is None or interval < 0:
        problems.append("Sample interval must be a finite number of zero or more.")
    soak = _finite_number(profile.get("soak_seconds"))
    if soak is None or soak < 0:
        problems.append("Soak time must be a finite number of zero or more.")
    if profile.get("require_near_setpoint") is not True:
        problems.append(
            "Reference proximity to the requested set point is required for "
            "a valid calibration.")
    near_tolerance = _finite_number(profile.get("setpoint_tolerance"))
    if near_tolerance is None or near_tolerance <= 0:
        problems.append(
            "Set-point tolerance must be a finite number greater than zero.")
    if profile.get("on_timeout") not in ("record", "abort"):
        problems.append("If stability times out, choose either record or abort.")
    poll = _finite_number(poll_interval)
    if poll is not None and poll > 0 and window is not None:
        needed = MIN_STABILITY_SAMPLES * poll
        if 0 < window < needed:
            problems.append(
                f"The stability window ({window:g} s) is too short for the "
                f"286's scan rate (a reading about every {poll:g} s). "
                f"Stability needs at least {MIN_STABILITY_SAMPLES} readings in "
                f"the window, so use {needed:g} s or more - otherwise every "
                "set point would time out even with a perfectly steady bath.")
    if (poll is not None and poll > 0 and interval is not None and
            0 < interval < poll):
        problems.append(
            f"Samples are requested every {interval:g} s but the 286 is only "
            f"scanned every {poll:g} s, so samples would repeat. "
            f"Use {poll:g} s or more.")
    return problems


def _positive(value):
    number = _finite_number(value)
    return number is not None and number > 0


def result_evidence_counts(results):
    """Return (sample-complete, incomplete) counts without changing evidence."""
    complete = incomplete = 0
    for result in results:
        expected = getattr(result, "expected_samples", None)
        reference = getattr(result, "reference", {}) or {}
        duts = getattr(result, "duts", {}) or {}
        expected_duts = getattr(result, "expected_dut_channels", ()) or ()
        sample_counts_complete = (
            expected is None or (
                reference.get("n", 0) == expected and
                all(duts.get(channel, {}).get("n", 0) == expected
                    for channel in expected_duts)
            )
        )
        if not sample_counts_complete:
            incomplete += 1
        else:
            complete += 1
    return complete, incomplete


class SetPointResult:
    """Everything measured at one set point."""

    def __init__(self, setpoint, unit, tolerance=None, expected_samples=None,
                 expected_dut_channels=None, sample_stability_band=None,
                 setpoint_tolerance=None):
        object.__setattr__(self, "_sealed", False)
        self.setpoint = setpoint
        self.unit = unit
        self.tolerance = tolerance
        self.expected_samples = expected_samples
        self.expected_dut_channels = tuple(expected_dut_channels or ())
        self.sample_stability_band = sample_stability_band
        self.setpoint_tolerance = setpoint_tolerance
        self.setpoint_readback = None
        self.setpoint_readback_raw = ""
        self.setpoint_readback_unit_raw = ""
        self.setpoint_readback_unit = ""
        self.setpoint_confirmed = None
        self.setpoint_command = ""
        self.source_checks = []
        self.source_checks_valid = False
        self.stable = False
        self.stabilize_seconds = 0.0
        self.note = ""
        self.started = time.time()
        self.finished = None
        self.samples = []              # [{'t', 'ref', 'duts': {ch: v}}]
        self.stability_samples = []    # immutable device reference frames
        self.reference = {}            # mean/sd/n
        self.duts = {}                 # ch -> mean/sd/n/error

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError(
                "A summarised set-point result is sealed calibration evidence.")
        object.__setattr__(self, name, value)

    # ----------------------------------------------------------- analysis --
    @staticmethod
    def _stats(values):
        vals = [v for v in values if v is not None]
        if not vals:
            return {"mean": None, "sd": None, "n": 0, "min": None, "max": None}
        if any(_finite_number(value) is None for value in vals):
            raise ValueError("A non-finite value reached the statistics stage.")
        vals = [float(value) for value in vals]
        return {
            "mean": statistics.fmean(vals),
            # Sample SD is undefined for one observation.  Reporting 0 would
            # falsely imply perfect repeatability.
            "sd": statistics.stdev(vals) if len(vals) > 1 else None,
            "n": len(vals),
            "min": min(vals),
            "max": max(vals),
        }

    def summarise(self, dut_channels):
        self.reference = MappingProxyType(
            self._stats([s["ref"] for s in self.samples]))
        ref_mean = self.reference["mean"]
        dut_results = {}
        for ch in dut_channels:
            st = self._stats([s["duts"].get(ch) for s in self.samples])
            st["error"] = (None if st["mean"] is None or ref_mean is None
                           else st["mean"] - ref_mean)
            st["tolerance"] = self.tolerance
            st["in_tolerance"] = (
                None if st["error"] is None or self.tolerance is None
                else abs(st["error"]) <= self.tolerance)
            dut_results[ch] = MappingProxyType(st)
        self.duts = MappingProxyType(dut_results)
        self.finished = time.time()
        self.samples = tuple(_deep_freeze(sample) for sample in self.samples)
        self.stability_samples = tuple(
            _deep_freeze(sample) for sample in self.stability_samples)
        self.source_checks = tuple(
            _deep_freeze(check) for check in self.source_checks)
        object.__setattr__(self, "_sealed", True)
        return self

    @property
    def quality_issues(self):
        issues = []
        if self.setpoint_confirmed is not True:
            issues.append("set point not confirmed")
        if self.source_checks_valid is not True:
            issues.append("heat-source set point/unit not confirmed through sampling")
        if not self.stable:
            issues.append("reference not stable")
        expected = self.expected_samples
        if expected is not None and self.reference.get("n") != expected:
            issues.append("reference sample count incomplete")
        for channel in self.expected_dut_channels:
            if channel not in self.duts:
                issues.append(f"{channel} result missing")
        for channel, stats in self.duts.items():
            if expected is not None and stats.get("n") != expected:
                issues.append(f"{channel} sample count incomplete")
        ref_mean = self.reference.get("mean")
        if (self.setpoint_tolerance is not None and ref_mean is not None and
                abs(ref_mean - self.setpoint) > self.setpoint_tolerance):
            issues.append("sampled reference mean outside set-point tolerance")
        ref_min = self.reference.get("min")
        ref_max = self.reference.get("max")
        if (self.sample_stability_band is not None and ref_min is not None and
                ref_max is not None and
                ref_max - ref_min > self.sample_stability_band):
            issues.append("sampled reference span exceeds stability band")
        return issues

    @property
    def verdict(self):
        """'pass', 'fail', or '' when tolerance was not set."""
        if self.quality_issues:
            return "invalid"
        checked = [d.get("in_tolerance") for d in self.duts.values()
                   if d.get("in_tolerance") is not None]
        if not checked:
            return ""
        return "pass" if all(checked) else "fail"


class RunEngine:
    """Drives one calibration from start to finish on its own thread."""

    _FINAL_EVIDENCE_FIELDS = frozenset({
        "run_id", "profile", "heat_source", "adt", "results", "state",
        "error", "evidence", "measurement_unit", "started_at", "finished_at",
    })

    def __setattr__(self, name, value):
        if (getattr(self, "_sealed_run", False) and
                (name == "_sealed_run" or
                 name in RunEngine._FINAL_EVIDENCE_FIELDS)):
            raise AttributeError(
                "This calibration run is finalized evidence; create a new "
                "RunEngine instead of replacing its certificate fields.")
        object.__setattr__(self, name, value)

    def __init__(self, run_id, profile, heat_source, adt, registry,
                 event_cb=None):
        object.__setattr__(self, "_sealed_run", False)
        self.run_id = run_id
        # Freeze nested lists/settings for the lifetime of the run.  Editing a
        # saved profile while a thread is active must not change its evidence.
        self.profile = copy.deepcopy(profile)
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
        self.evidence = MappingProxyType({})
        self.measurement_unit = ""

        self._stop = threading.Event()
        self._thread = None
        self._current_result = None

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
    def tolerance(self):
        """Tolerance for the point being measured right now."""
        return tolerance_at(self.profile, max(self.current_index, 0))

    @property
    def channels(self):
        ref = self.profile.get("reference_channel")
        return ([ref] if ref else []) + list(self.profile.get("dut_channels", []))

    @property
    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_active or self.state == STATE_RUNNING:
            raise RuntimeError("This run is already going.")
        if self.state in (STATE_DONE, STATE_ABORTED, STATE_ERROR):
            raise RuntimeError(
                "A finalized calibration cannot be restarted or overwritten. "
                "Create a new run from the profile instead.")
        if not getattr(self.adt, "is_open", False):
            raise RuntimeError("The ADT286 is not connected.")
        refresh_unit = getattr(self.heat_source, "refresh_reported_unit", None)
        if callable(refresh_unit):
            try:
                refresh_unit()
            except Exception as exc:
                raise RuntimeError(
                    f"Could not read {self.heat_source.name}'s current "
                    "temperature unit before validating the run: "
                    f"{exc}") from exc
        available = list(getattr(self.adt, "channels", []))
        problems = validate_profile(
            self.profile, self.heat_source, available,
            getattr(self.adt, "poll_interval", None),
            getattr(self.adt, "unit", ""))
        if problems:
            raise ValueError("; ".join(problems))
        # Validation is the last point at which profile settings may vary.  A
        # run and every later certificate use this recursively immutable copy.
        self.profile = _deep_freeze(self.profile)
        self.measurement_unit = getattr(self.adt, "unit", "")
        channel_config = {}
        for channel in self.channels:
            info = getattr(self.adt, "channel_info", {}).get(channel, {})
            channel_config[channel] = _deep_freeze(copy.deepcopy(info))
        try:
            source_connection = self.heat_source.connection
        except Exception:
            source_connection = ""
        self.evidence = _deep_freeze({
            "readout": "Additel ADT286",
            "readout_identity": str(getattr(self.adt, "idn", "") or ""),
            "readout_connection": str(
                getattr(getattr(self.adt, "link", None), "describe",
                        lambda: "")() or ""),
            "readout_unit": self.measurement_unit,
            "channel_configuration": MappingProxyType(channel_config),
            "heat_source": self.heat_source.name,
            "heat_source_identity": str(self.heat_source.idn or ""),
            "heat_source_connection": str(source_connection or ""),
            "heat_source_unit": self.heat_source.unit,
            "heat_source_reported_unit": str(
                getattr(self.heat_source, "reported_unit", "") or ""),
            "heat_source_unit_reply": str(
                getattr(self.heat_source, "last_unit_reply", "") or ""),
            "heat_source_range_unit": self.heat_source.unit,
            "heat_source_range": tuple(self.heat_source.range),
            "acquisition_command": "SCAN:DATA:Last? 1",
        })
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
        if self.state != STATE_RUNNING:
            return False
        self._stop.set()
        self.error = self.error or reason
        return True

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # --------------------------------------------------------- internals ---
    def _sleep(self, seconds):
        """Interruptible wait. Returns False if a stop was requested."""
        return not self._stop.wait(seconds)

    def _verify_source_unit(self, context):
        """Refresh and compare device-reported source/readout units."""
        refresh = getattr(self.heat_source, "refresh_reported_unit", None)
        try:
            reported = refresh() if callable(refresh) else getattr(
                self.heat_source, "reported_unit", "")
        except Exception as exc:
            raise _SourceUnitVerificationError(
                f"{self.heat_source.name} could not read its temperature unit "
                f"{context}: {exc}. Nothing was sampled.") from exc
        unit_raw = str(getattr(
            self.heat_source, "last_unit_reply", "") or "")
        if not _normalise_unit(reported):
            raise _SourceUnitVerificationError(
                f"{self.heat_source.name} did not report its temperature "
                f"unit {context}. Nothing was sampled.", reported=reported,
                raw=unit_raw, query_succeeded=True)
        if (_normalise_unit(reported) !=
                _normalise_unit(self.measurement_unit)):
            raise _SourceUnitVerificationError(
                f"Heat-source unit mismatch {context}: "
                f"{self.heat_source.name} reports {reported}, while the "
                f"ADT286 reports {self.measurement_unit}. Nothing was "
                "sampled or converted.", reported=reported, raw=unit_raw,
                query_succeeded=True)
        return reported

    def _confirm_source_setpoint(self, result, expected, context,
                                 primary=False):
        """Record one live set-point/unit check and fail closed on mismatch."""
        ok, readback = self.heat_source.confirm_setpoint(expected)
        raw = str(getattr(
            self.heat_source, "last_setpoint_readback_raw", "") or "")
        readback_unit_raw = str(getattr(
            self.heat_source, "last_setpoint_readback_unit_raw", "") or "")
        readback_unit = str(getattr(
            self.heat_source, "last_setpoint_readback_unit", "") or "")
        if primary:
            result.setpoint_readback = readback
            result.setpoint_readback_raw = raw
            result.setpoint_readback_unit_raw = readback_unit_raw
            result.setpoint_readback_unit = readback_unit
            result.setpoint_confirmed = ok

        def recorded_check(reported_unit, unit_raw, verified=True,
                           query_succeeded=True):
            return MappingProxyType({
                "t": time.time(), "context": context, "expected": expected,
                "readback": readback, "raw": raw,
                "readback_unit_raw": readback_unit_raw,
                "readback_unit": readback_unit,
                "unit": reported_unit,
                "unit_raw": unit_raw,
                "unit_verified": verified,
                "unit_query_succeeded": query_succeeded,
                "confirmed": ok is True,
            })

        try:
            reported_unit = self._verify_source_unit(context)
        except _SourceUnitVerificationError as exc:
            result.source_checks.append(recorded_check(
                exc.reported, exc.raw, verified=False,
                query_succeeded=exc.query_succeeded))
            raise
        except Exception:
            result.source_checks.append(recorded_check(
                "", "", verified=False, query_succeeded=False))
            raise
        live_unit_raw = str(getattr(
            self.heat_source, "last_unit_reply", "") or "")
        exact_unit = _normalise_unit(readback_unit)
        unit_problem = ""
        if readback_unit_raw and not exact_unit:
            unit_problem = (
                f"{self.heat_source.name} attached an unrecognised unit token "
                f"{readback_unit_raw!r} to its set-point reply. The reply was "
                "retained, but the point was not sampled or relabelled.")
        elif (exact_unit and
              (exact_unit != _normalise_unit(self.measurement_unit) or
               exact_unit != _normalise_unit(reported_unit))):
            unit_problem = (
                f"Set-point reply unit mismatch {context}: the exact reply "
                f"reports {readback_unit}, the live unit query reports "
                f"{reported_unit}, and the ADT286 reports "
                f"{self.measurement_unit}. The point was not sampled or "
                "relabelled.")
        check = recorded_check(
            reported_unit, live_unit_raw, verified=not unit_problem)
        result.source_checks.append(check)
        if readback is not None and not raw:
            raise RuntimeError(
                f"{self.heat_source.name} returned a numeric set point but "
                "its exact device reply was not retained. The point was not "
                "sampled.")
        if unit_problem:
            raise RuntimeError(unit_problem)
        if ok is False:
            raise RuntimeError(
                f"Set point readback is {readback}, expected {expected:g}. "
                "The point was not sampled because the heat source did not "
                "confirm the commanded value.")
        if ok is not True:
            raise RuntimeError(
                f"{self.heat_source.name} cannot confirm its set point. "
                "Configure a set-point read command before running a "
                "calibration.")
        return check

    def _fresh_reference(self, last_cycle):
        """Wait for a scan cycle newer than last_cycle. -> (cycle, reading)."""
        deadline = time.monotonic() + 15.0
        while not self._stop.is_set() and time.monotonic() < deadline:
            r = self.adt.latest(self.profile["reference_channel"])
            if r is not None and r.cycle > last_cycle and r.temperature is not None:
                if _finite_number(r.temperature) is None:
                    raise RuntimeError("The ADT286 returned a non-finite reference value.")
                if (_normalise_unit(r.unit) !=
                        _normalise_unit(self.measurement_unit)):
                    raise RuntimeError(
                        f"ADT286 unit changed during the run from "
                        f"{self.measurement_unit} to {r.unit}. No mixed-unit "
                        "sample was recorded.")
                if not r.raw_temperature:
                    raise RuntimeError(
                        "The ADT286 reference reply did not retain its raw "
                        "numeric token. The reading was not recorded.")
                return r.cycle, r
            self._stop.wait(0.2)
        return last_cycle, None

    def _fresh_sample_frame(self, last_cycle):
        """Wait for one complete, finite, same-cycle device scan frame."""
        deadline = time.monotonic() + 15.0
        candidate = last_cycle
        channels = self.channels
        while not self._stop.is_set() and time.monotonic() < deadline:
            reference = self.adt.latest(self.profile["reference_channel"])
            if (reference is None or reference.cycle <= candidate or
                    reference.temperature is None):
                self._stop.wait(0.1)
                continue
            candidate = reference.cycle
            frame = self.adt.snapshot(channels, cycle=candidate)
            if any(frame.get(channel) is None for channel in channels):
                self._stop.wait(0.1)
                continue
            for channel, reading in frame.items():
                if _finite_number(reading.temperature) is None:
                    raise RuntimeError(
                        f"The ADT286 returned a non-finite value on {channel}.")
                if (_normalise_unit(reading.unit) !=
                        _normalise_unit(self.measurement_unit)):
                    raise RuntimeError(
                        f"ADT286 unit changed during the run on {channel}: "
                        f"expected {self.measurement_unit}, received "
                        f"{reading.unit}. No mixed-unit sample was recorded.")
                if not reading.raw_temperature:
                    raise RuntimeError(
                        f"The ADT286 reply for {channel} did not retain its "
                        "raw numeric token. The frame was not recorded.")
            return candidate, frame
        return candidate, None

    def _stabilize(self, setpoint, result=None):
        """Hold until the reference is flat within the band over the window."""
        band = float(self.profile["stability_band"])
        window = float(self.profile["stability_window"])
        max_wait = float(self.profile["max_wait"])
        near = True
        sp_tol = float(self.profile.get("setpoint_tolerance", 1.0))

        history = deque()          # (timestamp, value) for the trailing window
        start = time.monotonic()
        watching_since = None      # first reading seen at this set point
        # Do not let a cached reading from before the set-point command start
        # the stability window.
        last_cycle = self.adt.cycle
        consecutive_data_timeouts = 0
        self._set_phase(PHASE_STABILIZING)
        while not self._stop.is_set():
            elapsed = time.monotonic() - start
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
                consecutive_data_timeouts += 1
                if consecutive_data_timeouts >= 3:
                    raise RuntimeError(
                        "Reference data was unavailable for three consecutive "
                        "acquisition timeouts. The point was not recorded.")
                self._log("WARN", "No reference data from the 286 — is the "
                                  "channel enabled and the probe connected?")
                if not self._sleep(1.0):
                    break
                continue
            consecutive_data_timeouts = 0
            now = reading.monotonic or time.monotonic()
            if watching_since is None:
                watching_since = now
            self.last_reference = reading.temperature
            if result is not None:
                result.stability_samples.append(MappingProxyType({
                    "t": reading.timestamp,
                    "cycle": reading.cycle,
                    "source": reading.source,
                    "device_timestamp": reading.device_timestamp,
                    "ref": reading.temperature,
                    "ref_raw": reading.raw_temperature,
                    "unit": reading.unit,
                }))
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
                    return True, time.monotonic() - start, ""
            if not self._sleep(0.2):
                break
        return False, time.monotonic() - start, "stopped"

    def _take_samples(self, result):
        count = int(self.profile["sample_count"])
        interval = float(self.profile["sample_interval"])
        duts = list(self.profile["dut_channels"])
        ref_ch = self.profile["reference_channel"]
        self._set_phase(PHASE_SAMPLING)
        # Sampling begins with the next physical scan, never a cached frame
        # from stability/soak.
        last_cycle = self.adt.cycle
        for i in range(count):
            if self._stop.is_set():
                return False
            source_check = self._confirm_source_setpoint(
                result, result.setpoint,
                f"before sample {i + 1} at {result.setpoint:g} {result.unit}")
            last_cycle, snap = self._fresh_sample_frame(last_cycle)
            if snap is None:
                raise RuntimeError(
                    f"Sample {i + 1} was not recorded: the ADT286 did not "
                    "provide every required channel in one fresh scan cycle.")
            reference = snap[ref_ch]
            dut_values = MappingProxyType(
                {channel: snap[channel].temperature for channel in duts})
            dut_raw = MappingProxyType(
                {channel: snap[channel].raw_temperature for channel in duts})
            units = MappingProxyType(
                {channel: snap[channel].unit for channel in [ref_ch] + duts})
            device_timestamps = MappingProxyType(
                {channel: snap[channel].device_timestamp
                 for channel in [ref_ch] + duts})
            sample = MappingProxyType({
                "t": reference.timestamp,
                "cycle": last_cycle,
                "source": reference.source,
                "device_timestamp": reference.device_timestamp,
                "device_timestamps": device_timestamps,
                "ref": reference.temperature,
                "ref_raw": reference.raw_temperature,
                "duts": dut_values,
                "duts_raw": dut_raw,
                "units": units,
                "source_setpoint": source_check["readback"],
                "source_setpoint_raw": source_check["raw"],
                "source_setpoint_unit_raw": source_check["readback_unit_raw"],
                "source_setpoint_unit": source_check["readback_unit"],
                "source_setpoint_confirmed": source_check["confirmed"],
                "source_unit_raw": source_check["unit_raw"],
                "source_verified_unit": source_check["unit"],
                "source_unit_query_succeeded": source_check[
                    "unit_query_succeeded"],
                "source_unit_verified": source_check["unit_verified"],
            })
            result.samples.append(sample)
            self._emit("sample", index=i + 1, of=count, sample=sample,
                       setpoint=result.setpoint)
            if i < count - 1 and not self._sleep(interval):
                return False
        return True

    def _preserve_incomplete_result(self, result, index, reason):
        """Retain any acquired device evidence when a point is interrupted."""
        if (result is None or result in self.results or not (
                result.samples or result.stability_samples or
                result.source_checks or result.setpoint_command)):
            return
        result.note = "; ".join(filter(None, [result.note, reason]))
        result.summarise(self.profile["dut_channels"])
        self.results.append(result)
        self._emit("result", result=result, index=index)

    def _run(self):
        hs = self.heat_source
        unit = self.measurement_unit or hs.unit
        try:
            self._verify_source_unit("before the run")
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
                tolerance = tolerance_at(self.profile, idx)
                result = SetPointResult(
                    sp, unit, tolerance,
                    expected_samples=int(self.profile["sample_count"]),
                    expected_dut_channels=self.profile["dut_channels"],
                    sample_stability_band=float(
                        self.profile["stability_band"]),
                    setpoint_tolerance=float(
                        self.profile["setpoint_tolerance"]))
                self._current_result = result

                self._set_phase(PHASE_SETTING)
                self._verify_source_unit(
                    f"before set point {sp:g} {unit}")
                sent = hs.set_setpoint(sp, send_password=self.profile.get(
                    "send_password", False))
                result.setpoint_command = str(
                    getattr(hs, "last_setpoint_command", "") or "")
                if sent is not True:
                    raise RuntimeError(
                        f"{hs.name} did not confirm that the set-point command "
                        "was sent. The point was not sampled.")
                self._confirm_source_setpoint(
                    result, sp,
                    f"after command for set point {sp:g} {unit}",
                    primary=True)
                self._log("PASS", f"Set point {sp:g} {unit} confirmed.")

                stable, secs, note = self._stabilize(sp, result)
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
                self._confirm_source_setpoint(
                    result, sp,
                    f"after sampling set point {sp:g} {unit}")
                result.source_checks_valid = True
                result.summarise(self.profile["dut_channels"])
                failed = [ch for ch, d in result.duts.items()
                          if d.get("in_tolerance") is False]
                if failed:
                    self._log("FAIL", f"{sp:g} {unit}: out of tolerance on "
                                      + ", ".join(failed)
                                      + f" (±{result.tolerance:g})")
                elif result.verdict == "pass":
                    self._log("PASS", f"{sp:g} {unit}: all devices within "
                                      f"±{result.tolerance:g}")
                self.results.append(result)
                self._emit("result", result=result, index=idx)
                self._current_result = None
            else:
                if not self._stop.is_set() and self.state == STATE_RUNNING:
                    self.state = STATE_DONE
        except Exception as e:
            self._preserve_incomplete_result(
                self._current_result, self.current_index,
                f"point interrupted by acquisition error: {e}")
            self.state = STATE_ERROR
            self.error = str(e)
            self._log("FAIL", f"Run stopped: {e}")
        finally:
            self._preserve_incomplete_result(
                self._current_result, self.current_index,
                "point interrupted before completion")
            self._current_result = None
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
            except Exception as e:
                self._log("WARN", f"Could not unsubscribe the ADT286 run: {e}")
            try:
                self.registry.release(self.run_id)
            except Exception as e:
                self._log("WARN", f"Could not release channel ownership: {e}")
            # No caller can append, replace, or relabel finalized results.
            self.results = tuple(self.results)
            self.finished_at = time.time()
            self.current_setpoint = None
            # Seal before callbacks see the terminal state.
            object.__setattr__(self, "_sealed_run", True)
            self._set_phase(PHASE_FINISHED)
            self._emit("state", state=self.state)
            complete, incomplete = result_evidence_counts(self.results)
            summary = (f"Run {self.state}. {complete} sample-complete set "
                       "point(s) recorded.")
            if incomplete:
                summary += (f" {incomplete} incomplete evidence record(s) "
                            "preserved but not recorded as calibration points.")
            self._log("INFO", summary)
