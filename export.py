"""CSV export for finished (or partial) calibration runs."""

import csv
import os
import re
import tempfile
import threading
from datetime import datetime
from types import SimpleNamespace


_EXPORT_LOCK = threading.Lock()


def _stamp(t, precise=False):
    if not t:
        return ""
    value = datetime.fromtimestamp(t).astimezone()
    return (value.isoformat(timespec="microseconds") if precise
            else value.isoformat(timespec="seconds"))


def safe_name(text):
    return re.sub(r"[^\w\-]+", "_", (text or "run")).strip("_") or "run"


def _overall(engine):
    """One word for the whole run, for the top of the certificate."""
    if engine.state != "complete":
        return f"INCOMPLETE - run status is {engine.state}"
    expected_points = len(engine.profile.get("setpoints", []))
    if len(engine.results) != expected_points:
        return (f"INCOMPLETE - {len(engine.results)} of {expected_points} "
                "set points recorded")
    planned_setpoints = tuple(engine.profile.get("setpoints", ()))
    recorded_setpoints = tuple(
        getattr(result, "setpoint", None) for result in engine.results)
    if recorded_setpoints != planned_setpoints:
        return ("NOT VALID - recorded set-point sequence does not match "
                "the calibration profile")
    expected_duts = set(engine.profile.get("dut_channels", []))
    if any(set(getattr(result, "duts", {})) != expected_duts
           for result in engine.results):
        return "NOT VALID - one or more requested DUT results are missing"
    verdicts = [r.verdict for r in engine.results if r.verdict]
    if any(v == "invalid" for v in verdicts):
        return "NOT VALID - stability, set-point, or sample criteria were not met"
    if len(verdicts) != expected_points:
        return "NOT ASSESSED - one or more points have no valid verdict"
    if not verdicts:
        return "not assessed (no tolerance set)"
    if any(v == "fail" for v in verdicts):
        return "FAIL - at least one device outside tolerance"
    return "PASS - every device within tolerance"


def _metadata_rows(engine, adt):
    p = engine.profile
    hs = engine.heat_source
    evidence = dict(getattr(engine, "evidence", {}) or {})
    lo, hi = evidence.get("heat_source_range", hs.range)
    rows = [
        ["Calibration run", p.get("name", "")],
        ["Exported", datetime.now().astimezone().isoformat(timespec="seconds")],
        ["Started", _stamp(engine.started_at)],
        ["Finished", _stamp(engine.finished_at)],
        ["Status", engine.state + (f" ({engine.error})" if engine.error else "")],
        [],
        ["Heat source", evidence.get("heat_source", hs.name)],
        ["Heat source identity",
         evidence.get("heat_source_identity") or "(not reported)"],
        ["Heat source connection", evidence.get("heat_source_connection", "")],
        ["Heat source reported unit",
         evidence.get("heat_source_reported_unit") or "(not reported)"],
        ["Heat source unit reply (device text)",
         evidence.get("heat_source_unit_reply") or "(not reported)"],
        ["Heat source range",
         f"{lo:g} to {hi:g} "
         f"{evidence.get('heat_source_range_unit',
                         evidence.get('heat_source_unit', hs.unit))}"
         if lo is not None else "(not set)"],
        ["Readout", evidence.get("readout", "Additel ADT286")],
        ["Readout identity",
         evidence.get("readout_identity") or "(not reported)"],
        ["Readout connection", evidence.get("readout_connection", "")],
        ["Readout unit", evidence.get("readout_unit", "")],
        ["Acquisition command",
         evidence.get("acquisition_command", "SCAN:DATA:Last? 1")],
        ["Data policy",
         "Device values retained uncorrected; mean, sample SD, error and "
         "verdict are derived separately"],
        [],
        ["Reference channel", p.get("reference_channel", "")],
        ["DUT channels", ", ".join(p.get("dut_channels", []))],
        ["Set points", ", ".join(_raw(s) for s in p.get("setpoints", []))],
        ["Tolerance",
         (", ".join(f"±{_raw(v)}" for v in p.get("tolerances", []))
          if (p.get("tolerance_mode") == "per_point" and p.get("tolerances"))
          else f"±{_raw(p.get('tolerance', 0.05))} across the range")],
        ["Overall result", _overall(engine)],
        ["Stability band", f"{p.get('stability_band')} over "
                           f"{p.get('stability_window')} s"],
        ["Required reference proximity",
         f"±{p.get('setpoint_tolerance')} from requested set point"],
        ["Max wait per point", f"{p.get('max_wait')} s"],
        ["Samples per point", f"{p.get('sample_count')} every "
                              f"{p.get('sample_interval')} s"],
        ["Soak after stability", f"{p.get('soak_seconds', 0)} s"],
        [],
    ]
    configuration = evidence.get("channel_configuration", {}) or {}
    if configuration:
        insert_at = next((index for index, row in enumerate(rows)
                          if row and row[0] == "Set points"), len(rows))
        channel_rows = []
        for channel in ([p.get("reference_channel", "")]
                        + list(p.get("dut_channels", []))):
            cfg = configuration.get(channel, {}) or {}
            raw = cfg.get("raw") if hasattr(cfg, "get") else ""
            if not raw and hasattr(cfg, "items"):
                raw = "; ".join(
                    f"{key}={value}" for key, value in sorted(cfg.items()))
            channel_rows.append(
                [f"Channel configuration ({channel})", raw or "(not reported)"])
        rows[insert_at:insert_at] = channel_rows
    return rows


def write_summary(engine, adt, path):
    """One row per set point per DUT channel: the calibration table."""
    p = engine.profile
    duts = list(p.get("dut_channels", []))
    evidence = dict(getattr(engine, "evidence", {}) or {})
    unit = evidence.get("readout_unit") or "(unit not reported)"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for row in _metadata_rows(engine, adt):
            w.writerow(row)
        w.writerow([
            "Set point requested", "Set point command",
            "Set point readback (device text)", "Set point readback (parsed)",
            "Set point reply unit token (device text)",
            "Set point readback unit", "Set point confirmed",
            "Stable", "Stabilise (s)", "Note",
            f"Reference mean ({unit})", "Reference SD", "Reference n",
            "DUT channel", f"DUT mean ({unit})", "DUT SD", "DUT n",
            f"Error DUT-Reference ({unit})", f"Tolerance ({unit})", "Result",
        ])
        for r in engine.results:
            ref = r.reference or {}
            for ch in duts:
                d = r.duts.get(ch, {})
                point_verdict = r.verdict
                note = r.note
                if r.quality_issues:
                    note = "; ".join(filter(None, [note]
                                     + list(r.quality_issues)))
                w.writerow([
                    _raw(r.setpoint),
                    r.setpoint_command,
                    r.setpoint_readback_raw,
                    _raw(r.setpoint_readback),
                    r.setpoint_readback_unit_raw,
                    r.setpoint_readback_unit,
                    "yes" if r.setpoint_confirmed else "NO",
                    "yes" if r.stable else "NO",
                    _fmt(r.stabilize_seconds),
                    note,
                    _fmt(ref.get("mean")), _fmt(ref.get("sd")),
                    ref.get("n", 0),
                    ch,
                    _fmt(d.get("mean")), _fmt(d.get("sd")), d.get("n", 0),
                    _fmt(d.get("error")),
                    _fmt(d.get("tolerance")),
                    ("NOT VALID" if point_verdict == "invalid" else
                     "" if d.get("in_tolerance") is None else
                     ("PASS" if d["in_tolerance"] else "FAIL")),
                ])
    return path


def write_samples(engine, adt, path):
    """Every immutable device sample, with no calculated columns."""
    p = engine.profile
    duts = list(p.get("dut_channels", []))
    evidence = dict(getattr(engine, "evidence", {}) or {})
    unit = evidence.get("readout_unit") or "(unit not reported)"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for row in _metadata_rows(engine, adt):
            w.writerow(row)
        w.writerow(["Set point requested", "Phase", "Sample",
                    "Device acquisition time", "Host receipt time",
                    "Scan cycle", "Source", f"Reference ({unit})"]
                   + [f"{c} ({unit})" for c in duts]
                   + [f"{c} device acquisition time" for c in duts]
                   + ["Heat-source set point (device text)",
                      "Heat-source set point unit token (device text)",
                      "Heat-source set point unit",
                      "Set-point readback confirmed",
                      "Heat-source live unit query (device text)",
                      "Heat-source live unit query (parsed)",
                      "Live unit query succeeded",
                      "Unit evidence valid"])
        for r in engine.results:
            for check in getattr(r, "source_checks", ()):
                row = [_raw(r.setpoint), "source check",
                       check.get("context", ""), "",
                       _stamp(check.get("t"), precise=True), "",
                       "Heat-source set-point readback", ""]
                row += [""] * len(duts)
                row += [""] * len(duts)
                row += [check.get("raw", ""),
                        check.get("readback_unit_raw", ""),
                        check.get("readback_unit", ""),
                        "yes" if check.get("confirmed") else "NO",
                        check.get("unit_raw", ""), check.get("unit", ""),
                        "yes" if check.get("unit_query_succeeded") else "NO",
                        "yes" if check.get("unit_verified") else "NO"]
                w.writerow(row)
            for i, s in enumerate(getattr(r, "stability_samples", ()), 1):
                ref = s.get("ref_raw", "")
                row = [_raw(r.setpoint), "stability", i,
                       s.get("device_timestamp", ""),
                       _stamp(s["t"], precise=True), s.get("cycle", ""),
                       s.get("source", ""), ref] + [""] * (2 * len(duts)) + [
                           "", "", "", "", "", "", "", ""]
                w.writerow(row)
            for i, s in enumerate(r.samples, 1):
                ref = s.get("ref_raw", "")
                raw_duts = s.get("duts_raw", {})
                row = [_raw(r.setpoint), "sampling", i,
                       s.get("device_timestamp", ""),
                       _stamp(s["t"], precise=True),
                       s.get("cycle", ""), s.get("source", ""), ref]
                row += [raw_duts.get(c, "") for c in duts]
                device_times = s.get("device_timestamps", {})
                row += [device_times.get(c, "") for c in duts]
                row += [s.get("source_setpoint_raw", ""),
                        s.get("source_setpoint_unit_raw", ""),
                        s.get("source_setpoint_unit", ""),
                        ("yes" if s.get("source_setpoint_confirmed")
                         else "NO"),
                        s.get("source_unit_raw", ""),
                        s.get("source_verified_unit", ""),
                        ("yes" if s.get("source_unit_query_succeeded")
                         else "NO"),
                        "yes" if s.get("source_unit_verified") else "NO"]
                w.writerow(row)
    return path


def _fmt(value):
    if value is None:
        return ""
    return repr(float(value))


def _raw(value):
    """Round-trip numeric text for raw fallback values; never display-round."""
    return "" if value is None else repr(float(value))


def _engine_snapshot(engine):
    """Capture the finished run references used by both exported files."""
    heat_source = engine.heat_source
    source_range = getattr(heat_source, "range", (None, None))
    try:
        source_range = tuple(source_range)
    except TypeError:
        source_range = (None, None)
    source = SimpleNamespace(
        name=getattr(heat_source, "name", ""),
        range=source_range,
        unit=getattr(heat_source, "unit", ""),
    )
    return SimpleNamespace(
        profile=engine.profile,
        heat_source=source,
        evidence=getattr(engine, "evidence", None),
        results=tuple(engine.results),
        state=engine.state,
        error=engine.error,
        started_at=engine.started_at,
        finished_at=engine.finished_at,
    )


def _unused_export_paths(folder, base, stamp):
    suffix = ""
    attempt = 1
    while True:
        stem = f"{base}_{stamp}{suffix}"
        summary = os.path.join(folder, f"{stem}_summary.csv")
        samples = os.path.join(folder, f"{stem}_samples.csv")
        if not os.path.exists(summary) and not os.path.exists(samples):
            return stem, summary, samples
        attempt += 1
        suffix = f"_{attempt}"


def _temporary_path(folder, stem, kind):
    descriptor, path = tempfile.mkstemp(
        dir=folder, prefix=f".{stem}_{kind}_", suffix=".tmp")
    os.close(descriptor)
    return path


def _remove_files(paths):
    """Best-effort removal used only for this invocation's exact paths."""
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _publish_export_pair(temporary_paths, final_paths):
    """Publish two complete files without replacing an existing file."""
    published = []
    try:
        for temporary, final in zip(temporary_paths, final_paths):
            # The already-complete file becomes visible in one filesystem
            # operation, and an existing target makes the link fail.
            os.link(temporary, final)
            published.append(final)
    except BaseException:
        _remove_files(published)
        raise
    finally:
        _remove_files(temporary_paths)


def export_run(engine, adt, folder):
    """Write both CSVs. Returns [summary_path, samples_path]."""
    if getattr(engine, "is_active", False):
        raise RuntimeError(
            "This calibration is still running. Stop or finish it before "
            "exporting so the summary and device-reading files describe one "
            "immutable result set.")
    snapshot = _engine_snapshot(engine)
    if getattr(engine, "is_active", False):
        raise RuntimeError(
            "This calibration started running while its export snapshot was "
            "being captured. Stop or finish it before exporting.")

    os.makedirs(folder, exist_ok=True)
    base = safe_name(snapshot.profile.get("name", "run"))
    stamp = datetime.fromtimestamp(
        snapshot.started_at or datetime.now().timestamp()).strftime(
            "%Y%m%d_%H%M%S")

    with _EXPORT_LOCK:
        stem, summary, samples = _unused_export_paths(folder, base, stamp)
        temporary_paths = []
        try:
            temporary_paths.append(_temporary_path(folder, stem, "summary"))
            temporary_paths.append(_temporary_path(folder, stem, "samples"))
            write_summary(snapshot, adt, temporary_paths[0])
            write_samples(snapshot, adt, temporary_paths[1])
            _publish_export_pair(temporary_paths, (summary, samples))
        except BaseException:
            _remove_files(temporary_paths)
            raise
    return [summary, samples]
