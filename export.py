"""CSV export for finished (or partial) calibration runs."""

import csv
import os
import re
from datetime import datetime


def _stamp(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def safe_name(text):
    return re.sub(r"[^\w\-]+", "_", (text or "run")).strip("_") or "run"


def _overall(engine):
    """One word for the whole run, for the top of the certificate."""
    verdicts = [r.verdict for r in engine.results if r.verdict]
    if not verdicts:
        return "not assessed (no tolerance set)"
    if any(v == "fail" for v in verdicts):
        return "FAIL - at least one device outside tolerance"
    return "PASS - every device within tolerance"


def _metadata_rows(engine, adt):
    p = engine.profile
    hs = engine.heat_source
    lo, hi = hs.range
    return [
        ["Calibration run", p.get("name", "")],
        ["Exported", _stamp(None) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Started", _stamp(engine.started_at)],
        ["Finished", _stamp(engine.finished_at)],
        ["Status", engine.state + (f" ({engine.error})" if engine.error else "")],
        [],
        ["Heat source", hs.name],
        ["Heat source identity", hs.idn or "(not reported)"],
        ["Heat source port", hs.profile.get("port", "")],
        ["Heat source range",
         f"{lo:g} to {hi:g} {hs.unit}" if lo is not None else "(not set)"],
        ["Readout", "Additel ADT286"],
        ["Readout identity", getattr(adt, "idn", "") or "(not reported)"],
        ["Readout unit", getattr(adt, "unit", "") or ""],
        [],
        ["Reference channel", p.get("reference_channel", "")],
        ["DUT channels", ", ".join(p.get("dut_channels", []))],
        ["Set points", ", ".join(f"{s:g}" for s in p.get("setpoints", []))],
        ["Tolerance",
         (", ".join(f"±{v:g}" for v in p.get("tolerances", []))
          if (p.get("tolerance_mode") == "per_point" and p.get("tolerances"))
          else f"±{p.get('tolerance', 0.05)} across the range")],
        ["Overall result", _overall(engine)],
        ["Stability band", f"{p.get('stability_band')} over "
                           f"{p.get('stability_window')} s"],
        ["Max wait per point", f"{p.get('max_wait')} s"],
        ["Samples per point", f"{p.get('sample_count')} every "
                              f"{p.get('sample_interval')} s"],
        ["Soak after stability", f"{p.get('soak_seconds', 0)} s"],
        [],
    ]


def write_summary(engine, adt, path):
    """One row per set point per DUT channel: the calibration table."""
    p = engine.profile
    duts = list(p.get("dut_channels", []))
    unit = getattr(adt, "unit", "") or "°C"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for row in _metadata_rows(engine, adt):
            w.writerow(row)
        w.writerow([
            "Set point", "Stable", "Stabilise (s)", "Note",
            f"Reference mean ({unit})", "Reference SD", "Reference n",
            "DUT channel", f"DUT mean ({unit})", "DUT SD", "DUT n",
            f"Error DUT-Reference ({unit})", f"Tolerance ({unit})", "Result",
        ])
        for r in engine.results:
            ref = r.reference or {}
            for ch in duts:
                d = r.duts.get(ch, {})
                w.writerow([
                    f"{r.setpoint:g}",
                    "yes" if r.stable else "NO",
                    f"{r.stabilize_seconds:.0f}",
                    r.note,
                    _fmt(ref.get("mean")), _fmt(ref.get("sd"), 5),
                    ref.get("n", 0),
                    ch,
                    _fmt(d.get("mean")), _fmt(d.get("sd"), 5), d.get("n", 0),
                    _fmt(d.get("error")),
                    _fmt(d.get("tolerance")),
                    "" if d.get("in_tolerance") is None
                    else ("PASS" if d["in_tolerance"] else "FAIL"),
                ])
    return path


def write_samples(engine, adt, path):
    """Every raw sample, one row per sample."""
    p = engine.profile
    duts = list(p.get("dut_channels", []))
    unit = getattr(adt, "unit", "") or "°C"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for row in _metadata_rows(engine, adt):
            w.writerow(row)
        w.writerow(["Set point", "Sample", "Timestamp",
                    f"Reference ({unit})"]
                   + [f"{c} ({unit})" for c in duts]
                   + [f"{c} error" for c in duts])
        for r in engine.results:
            for i, s in enumerate(r.samples, 1):
                ref = s.get("ref")
                row = [f"{r.setpoint:g}", i, _stamp(s["t"]), _fmt(ref)]
                row += [_fmt(s["duts"].get(c)) for c in duts]
                row += [_fmt(None if (s["duts"].get(c) is None or ref is None)
                             else s["duts"][c] - ref) for c in duts]
                w.writerow(row)
    return path


def _fmt(value, places=4):
    if value is None:
        return ""
    return f"{value:.{places}f}"


def export_run(engine, adt, folder):
    """Write both CSVs. Returns [summary_path, samples_path]."""
    os.makedirs(folder, exist_ok=True)
    base = safe_name(engine.profile.get("name", "run"))
    stamp = datetime.fromtimestamp(
        engine.started_at or datetime.now().timestamp()).strftime("%Y%m%d_%H%M")
    summary = os.path.join(folder, f"{base}_{stamp}_summary.csv")
    samples = os.path.join(folder, f"{base}_{stamp}_samples.csv")
    write_summary(engine, adt, summary)
    write_samples(engine, adt, samples)
    return [summary, samples]
