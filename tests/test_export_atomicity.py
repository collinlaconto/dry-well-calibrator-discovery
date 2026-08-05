import csv
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.export import _overall, export_run, write_summary


def _result(setpoint, *, tolerance=0.01):
    return SimpleNamespace(
        setpoint=setpoint,
        setpoint_command=f"SET {setpoint!r}",
        setpoint_readback_raw=repr(float(setpoint)),
        setpoint_readback=float(setpoint),
        setpoint_readback_unit="°C",
        setpoint_readback_unit_raw="C",
        setpoint_confirmed=True,
        stable=True,
        stabilize_seconds=0.123456789012345,
        note="",
        quality_issues=(),
        reference=MappingProxyType({
            "mean": 20.123456789012345,
            "sd": 1.234567890123456e-09,
            "n": 3,
        }),
        duts=MappingProxyType({
            "DUT": MappingProxyType({
                "mean": 20.123456789013333,
                "sd": 5.67890123456789e-10,
                "n": 3,
                "error": 9.876543210987654e-13,
                "tolerance": tolerance,
                "in_tolerance": True,
            }),
        }),
        verdict="pass",
        source_checks=(),
        stability_samples=(),
        samples=(),
    )


def _engine(*, setpoints=(20.0,), results=None, tolerance=0.01):
    profile = MappingProxyType({
        "name": "Atomic evidence run",
        "reference_channel": "REF",
        "dut_channels": ("DUT",),
        "setpoints": tuple(setpoints),
        "tolerance_mode": "per_point",
        "tolerances": tuple(tolerance for _ in setpoints),
        "tolerance": tolerance,
        "stability_band": 1.234567890123456e-09,
        "stability_window": 60.0,
        "setpoint_tolerance": 9.876543210987654e-10,
        "max_wait": 600.0,
        "sample_count": 3,
        "sample_interval": 1.0,
        "soak_seconds": 0.0,
    })
    if results is None:
        results = tuple(_result(point, tolerance=tolerance)
                        for point in setpoints)
    return SimpleNamespace(
        profile=profile,
        heat_source=SimpleNamespace(
            name="Test Well", range=(-100.0, 200.0), unit="°C"),
        evidence=MappingProxyType({"readout_unit": "°C"}),
        results=tuple(results),
        state="complete",
        error="",
        is_active=False,
        started_at=1_700_000_000.0,
        finished_at=1_700_000_001.0,
    )


def _read_csv(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


class ExportCertificateIntegrityTests(unittest.TestCase):
    def test_overall_rejects_reordered_or_substituted_setpoints(self):
        planned = (10.0, 20.0)
        cases = (
            (_result(20.0), _result(10.0)),
            (_result(10.0), _result(10.0)),
            (_result(10.0), _result(30.0)),
        )
        for results in cases:
            with self.subTest(recorded=tuple(r.setpoint for r in results)):
                verdict = _overall(_engine(
                    setpoints=planned, results=results))
                self.assertIn("NOT VALID", verdict)
                self.assertIn("sequence", verdict)

    def test_certificate_preserves_full_precision(self):
        tolerance = 1.234567890123456e-12
        engine = _engine(tolerance=tolerance)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.csv"
            write_summary(engine, None, str(path))
            rows = _read_csv(path)

        metadata = {row[0]: row[1] for row in rows if len(row) >= 2}
        self.assertIn(repr(tolerance), metadata["Tolerance"])

        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == "Set point requested"
            and "Reference mean (°C)" in row)
        header = rows[header_index]
        data = rows[header_index + 1]
        expected = {
            "Stabilise (s)": 0.123456789012345,
            "Reference mean (°C)": 20.123456789012345,
            "Reference SD": 1.234567890123456e-09,
            "DUT mean (°C)": 20.123456789013333,
            "DUT SD": 5.67890123456789e-10,
            "Error DUT-Reference (°C)": 9.876543210987654e-13,
            "Tolerance (°C)": tolerance,
        }
        for column, value in expected.items():
            with self.subTest(column=column):
                self.assertEqual(data[header.index(column)], repr(float(value)))


class AtomicExportTests(unittest.TestCase):
    def test_failed_second_write_leaves_no_final_or_temporary_file(self):
        engine = _engine()
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch(
                    "calsuite.export.write_samples",
                    side_effect=OSError("simulated evidence write failure")):
                with self.assertRaisesRegex(OSError, "evidence write failure"):
                    export_run(engine, None, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_failed_second_publish_rolls_back_first_file(self):
        engine = _engine()
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated publish failure")
            return real_link(source, destination)

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("calsuite.export.os.link",
                            side_effect=fail_second_link):
                with self.assertRaisesRegex(OSError, "publish failure"):
                    export_run(engine, None, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_concurrent_exports_get_complete_unique_pairs(self):
        engine = _engine()
        count = 12
        with tempfile.TemporaryDirectory() as folder:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(export_run, engine, None, folder)
                           for _ in range(count)]
                pairs = [future.result(timeout=10) for future in futures]

            paths = [path for pair in pairs for path in pair]
            self.assertEqual(len(paths), count * 2)
            self.assertEqual(len(set(paths)), count * 2)
            self.assertTrue(all(Path(path).stat().st_size > 0 for path in paths))
            self.assertEqual(
                {Path(path) for path in paths}, set(Path(folder).iterdir()))
            for summary, samples in pairs:
                self.assertTrue(summary.endswith("_summary.csv"))
                self.assertTrue(samples.endswith("_samples.csv"))
                self.assertEqual(
                    summary.removesuffix("_summary.csv"),
                    samples.removesuffix("_samples.csv"),
                )


if __name__ == "__main__":
    unittest.main()
