"""Closure regressions for derived series and immutable export evidence."""

import csv
import math
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from tests.bootstrap import bootstrap_calsuite
from tests.test_export_integrity import export_fixture

bootstrap_calsuite()

from calsuite.datasync import build_series, convert_series
from calsuite.export import export_run, write_samples, write_summary


DEG_C = "\N{DEGREE SIGN}C"


def read_csv(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def table_after_header(rows, required_column):
    index = next(
        index for index, row in enumerate(rows)
        if row and row[0] == "Set point requested" and required_column in row
    )
    return rows[index], rows[index + 1:]


class SeriesClosureTests(unittest.TestCase):
    def test_build_series_discards_both_infinities_without_mutating_input(self):
        times = pd.to_datetime([
            "2026-08-05T12:00:00",
            "2026-08-05T12:00:01",
            "2026-08-05T12:00:02",
            "2026-08-05T12:00:03",
            "2026-08-05T12:00:04",
        ])
        frame = pd.DataFrame({
            "when": times,
            "temperature": ["20.125", math.inf, -math.inf, "NaN", "20.625"],
        })
        original = frame.copy(deep=True)
        time_info = {
            "kind": "absolute",
            "col": "when",
            "parsed": pd.Series(times),
        }

        derived = build_series(frame, time_info, "temperature")

        self.assertEqual(derived["_value"].tolist(), [20.125, 20.625])
        self.assertTrue(derived["_value"].map(math.isfinite).all())
        assert_frame_equal(frame, original)

    def test_convert_series_rejects_unknown_source_and_target_units(self):
        frame = pd.DataFrame({
            "_abs_time": pd.to_datetime(["2026-08-05T12:00:00"]),
            "_value": [68.0],
        })
        original = frame.copy(deep=True)

        for unit in ("Rankine", "degrees X"):
            with self.subTest(source_unit=unit):
                with self.assertRaisesRegex(ValueError, "temperature unit"):
                    convert_series(frame, unit, target="C")

        with self.assertRaisesRegex(ValueError, "conversion to Celsius"):
            convert_series(frame, "C", target="Rankine")
        assert_frame_equal(frame, original)


class ExportClosureTests(unittest.TestCase):
    def test_timestamp_omission_exports_blank_device_time_and_host_receipt(self):
        def add_timestamp_free_sample(result):
            result.source_checks = ()
            result.stability_samples = ()
            result.samples = (
                MappingProxyType({
                    "t": 1_700_000_000.123456,
                    "device_timestamp": "",
                    "device_timestamps": MappingProxyType({"DUT": ""}),
                    "cycle": 7,
                    "source": "ADT286 SCAN:DATA:Last? 1",
                    "ref": 20.0,
                    "ref_raw": "20.000000000000000001",
                    "duts": MappingProxyType({"DUT": 20.1}),
                    "duts_raw": MappingProxyType({
                        "DUT": "20.100000000000000001",
                    }),
                }),
            )

        engine, adt = export_fixture(add_timestamp_free_sample)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "timestamp-free.csv"
            write_samples(engine, adt, str(path))
            rows = read_csv(path)

        header, data = table_after_header(rows, "Phase")
        row = next(item for item in data if item[header.index("Phase")]
                   == "sampling")
        self.assertEqual(row[header.index("Device acquisition time")], "")
        self.assertEqual(row[header.index("DUT device acquisition time")], "")
        self.assertNotEqual(row[header.index("Host receipt time")], "")
        self.assertEqual(row[header.index(f"Reference ({DEG_C})")],
                         "20.000000000000000001")

    def test_raw_export_never_reconstructs_missing_device_tokens(self):
        source_checks = (
            MappingProxyType({
                "t": 1_700_000_000.100001,
                "context": "before sample 1 at 20 C",
                "expected": 20.0,
                    "readback": 20.0,
                    "raw": "20.000000000000000001",
                    "readback_unit_raw": "C",
                    "readback_unit": DEG_C,
                    "unit": DEG_C,
                    "unit_query_succeeded": True,
                    "unit_verified": True,
                    "confirmed": False,
            }),
        )
        stability_samples = (
            MappingProxyType({
                "t": 1_700_000_000.100002,
                "device_timestamp": "2026:08:05 12:00:00 100",
                "cycle": 40,
                "source": "ADT286 SCAN:DATA:Last? 1",
                "ref": 19.999999,
                "ref_raw": "",
                "unit": DEG_C,
            }),
        )
        samples = (
            MappingProxyType({
                "t": 1_700_000_000.100003,
                "device_timestamp": "2026:08:05 12:00:01 100",
                "cycle": 41,
                "source": "ADT286 SCAN:DATA:Last? 1",
                "ref": 20.123456,
                "ref_raw": "",
                "duts": MappingProxyType({"DUT": 20.223456}),
                "duts_raw": MappingProxyType({}),
                "units": MappingProxyType({"REF": DEG_C, "DUT": DEG_C}),
                    "source_setpoint": 20.0,
                    "source_setpoint_raw": "",
                    "source_setpoint_unit_raw": "C",
                    "source_setpoint_unit": DEG_C,
                    "source_setpoint_confirmed": True,
                    "source_unit_raw": "C",
                    "source_verified_unit": DEG_C,
                    "source_unit_query_succeeded": True,
                    "source_unit_verified": True,
            }),
        )

        def add_raw_evidence(result):
            result.source_checks = source_checks
            result.stability_samples = stability_samples
            result.samples = samples

        engine, adt = export_fixture(add_raw_evidence)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw-evidence.csv"
            write_samples(engine, adt, str(path))
            rows = read_csv(path)

        header, data = table_after_header(rows, "Phase")
        phase = header.index("Phase")
        source = header.index("Source")
        reference = header.index(f"Reference ({DEG_C})")
        dut = header.index(f"DUT ({DEG_C})")
        source_raw = header.index("Heat-source set point (device text)")
        source_unit = header.index("Heat-source set point unit")
        source_confirmed = header.index("Set-point readback confirmed")
        by_phase = {row[phase]: row for row in data}

        self.assertEqual(set(by_phase), {"source check", "stability", "sampling"})
        self.assertEqual(
            by_phase["source check"][source],
            "Heat-source set-point readback",
        )
        self.assertEqual(
            by_phase["source check"][source_raw],
            "20.000000000000000001",
        )
        self.assertEqual(by_phase["source check"][source_unit], DEG_C)
        self.assertEqual(by_phase["source check"][source_confirmed], "NO")

        # Numeric parsed values exist in memory, but absent device text must
        # remain absent in the raw evidence file rather than being recreated.
        self.assertEqual(by_phase["stability"][reference], "")
        self.assertEqual(by_phase["sampling"][reference], "")
        self.assertEqual(by_phase["sampling"][dut], "")
        self.assertEqual(by_phase["sampling"][source_raw], "")
        self.assertEqual(by_phase["sampling"][source_unit], DEG_C)
        self.assertEqual(by_phase["sampling"][source_confirmed], "yes")

    def test_summary_exports_device_setpoint_fields_verbatim(self):
        def add_device_fields(result, raw):
            result.setpoint_command = "SET 20.000000000000000000"
            result.setpoint_readback = 20.0
            result.setpoint_readback_raw = raw
            result.setpoint_readback_unit = DEG_C

        engine, adt = export_fixture(
            lambda result: add_device_fields(
                result, "20.000000000000000001"))

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.csv"
            write_summary(engine, adt, str(path))
            rows = read_csv(path)

        header, data = table_after_header(rows, "Set point command")
        row = data[0]
        self.assertEqual(
            row[header.index("Set point command")],
            "SET 20.000000000000000000",
        )
        self.assertEqual(
            row[header.index("Set point readback (device text)")],
            "20.000000000000000001",
        )
        self.assertEqual(
            row[header.index("Set point readback (parsed)")], "20.0"
        )
        self.assertEqual(row[header.index("Set point readback unit")], DEG_C)

        engine, adt = export_fixture(
            lambda result: add_device_fields(result, ""))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary-missing-token.csv"
            write_summary(engine, adt, str(path))
            rows = read_csv(path)
        header, data = table_after_header(rows, "Set point command")
        self.assertEqual(
            data[0][header.index("Set point readback (device text)")], ""
        )

    def test_channel_configuration_metadata_is_written_to_both_exports(self):
        engine, adt = export_fixture()
        evidence = dict(engine.evidence)
        evidence["channel_configuration"] = MappingProxyType({
            "REF": MappingProxyType({
                "raw": 'REF,1,"Reference",4,0,0,0,0,0,SPRT,REF-123',
                "serial": "REF-123",
            }),
            "DUT": MappingProxyType({
                "enabled": True,
                "sensor": "PRT",
                "serial": "DUT-456",
            }),
        })
        engine.evidence = MappingProxyType(evidence)

        with tempfile.TemporaryDirectory() as folder:
            paths = export_run(engine, adt, folder)
            exported = [read_csv(path) for path in paths]

        for rows in exported:
            with self.subTest(export=rows[0] if rows else "empty"):
                metadata = {
                    row[0]: row[1] for row in rows if len(row) >= 2
                }
                self.assertEqual(
                    metadata["Channel configuration (REF)"],
                    'REF,1,"Reference",4,0,0,0,0,0,SPRT,REF-123',
                )
                structured = metadata["Channel configuration (DUT)"]
                self.assertIn("enabled=True", structured)
                self.assertIn("sensor=PRT", structured)
                self.assertIn("serial=DUT-456", structured)


if __name__ == "__main__":
    unittest.main()
