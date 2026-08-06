"""Regressions for device-time alignment and explicit chart units."""

from datetime import datetime
import math
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite import datasync


def run_engine(samples, unit="C"):
    return SimpleNamespace(
        profile={"reference_channel": "REF", "dut_channels": ["DUT"]},
        evidence=MappingProxyType({"readout_unit": unit}),
        results=(SimpleNamespace(samples=tuple(samples)),),
    )


def sample(timestamp, ref=20.0, dut=20.1, host_time=4_102_444_800.0,
           reference_timestamp=None):
    per_channel = timestamp
    return MappingProxyType({
        "t": host_time,
        "device_timestamp": reference_timestamp or per_channel.get("REF", ""),
        "device_timestamps": MappingProxyType(dict(per_channel)),
        "ref": ref,
        "duts": MappingProxyType({"DUT": dut}),
    })


def analyzed_file(value_column="Temp C"):
    frame = pd.DataFrame({
        "When": ["2026-08-05 12:00:00"],
        value_column: ["20.0"],
    })
    return {
        "df": frame,
        "sheet": None,
        "time_info": {
            "kind": "absolute",
            "col": "When",
            "parsed": pd.to_datetime(frame["When"]),
        },
        "value_guess": value_column,
    }


class DeviceTimestampSeriesTests(unittest.TestCase):
    def test_selected_channel_uses_its_device_time_not_host_or_reference_time(self):
        evidence = sample(
            {
                "REF": "2026:08:05 12:00:01 123",
                "DUT": "2026:08:05 12:00:02 456",
            },
            host_time=4_102_444_800.0,
        )
        engine = run_engine((evidence,))

        reference = datasync.series_from_run(engine, "REF")
        dut = datasync.series_from_run(engine, "DUT")

        self.assertEqual(
            reference.loc[0, "_abs_time"].to_pydatetime(),
            datetime(2026, 8, 5, 12, 0, 1, 123000),
        )
        self.assertEqual(
            dut.loc[0, "_abs_time"].to_pydatetime(),
            datetime(2026, 8, 5, 12, 0, 2, 456000),
        )
        self.assertNotEqual(
            dut.loc[0, "_abs_time"].to_pydatetime(),
            datetime.fromtimestamp(evidence["t"]),
        )

    def test_documented_adt_timestamp_separators_and_whitespace_parse(self):
        tokens = (
            '  "2026:08:05   12:00:01   123"  ',
            "2026:08:05 12:00:02.456",
            "2026:08:05 12:00:03:789",
        )
        samples = tuple(
            sample({"REF": token, "DUT": token}, ref=20.0 + index)
            for index, token in enumerate(tokens)
        )

        frame = datasync.series_from_run(run_engine(samples), "REF")

        self.assertEqual(
            [value.to_pydatetime() for value in frame["_abs_time"]],
            [
                datetime(2026, 8, 5, 12, 0, 1, 123000),
                datetime(2026, 8, 5, 12, 0, 2, 456000),
                datetime(2026, 8, 5, 12, 0, 3, 789000),
            ],
        )

    def test_observed_hyphenated_device_times_control_alignment(self):
        later = sample(
            {"REF": "2026-08-06 16:58:23 861",
             "DUT": "2026-08-06 16:25:32 000"},
            ref=24.175371, dut=24.00675, host_time=1.0,
        )
        earlier = sample(
            {"REF": "2026-08-06 16:58:22 861",
             "DUT": "2026-08-06 16:25:31 000"},
            ref=24.075371, dut=23.90675, host_time=9_999_999_999.0,
        )

        reference = datasync.series_from_run(
            run_engine((later, earlier)), "REF")
        dut = datasync.series_from_run(run_engine((later, earlier)), "DUT")

        self.assertEqual(reference["_value"].tolist(), [24.075371, 24.175371])
        self.assertEqual(dut["_value"].tolist(), [23.90675, 24.00675])
        self.assertEqual(
            dut.loc[0, "_abs_time"].to_pydatetime(),
            datetime(2026, 8, 6, 16, 25, 31),
        )

    def test_device_time_controls_sort_order_not_host_receipt_time(self):
        later_device_time = sample(
            {"REF": "2026:08:05 12:00:02 000", "DUT": "2026:08:05 12:00:02 000"},
            ref=22.0,
            host_time=1.0,
        )
        earlier_device_time = sample(
            {"REF": "2026:08:05 12:00:01 000", "DUT": "2026:08:05 12:00:01 000"},
            ref=21.0,
            host_time=9_999_999_999.0,
        )

        frame = datasync.series_from_run(
            run_engine((later_device_time, earlier_device_time)), "REF")

        self.assertEqual(frame["_value"].tolist(), [21.0, 22.0])

    def test_reference_can_use_retained_top_level_device_token(self):
        evidence = MappingProxyType({
            "t": 4_102_444_800.0,
            "device_timestamp": "2026:08:05 12:00:01 123",
            "ref": 20.0,
            "duts": MappingProxyType({"DUT": 20.1}),
        })

        frame = datasync.series_from_run(run_engine((evidence,)), "REF")

        self.assertEqual(
            frame.loc[0, "_abs_time"].to_pydatetime(),
            datetime(2026, 8, 5, 12, 0, 1, 123000),
        )

    def test_missing_dut_device_time_never_falls_back_to_host_or_reference(self):
        evidence = sample(
            {"REF": "2026:08:05 12:00:01 123"},
            reference_timestamp="2026:08:05 12:00:01 123",
        )

        with self.assertRaisesRegex(ValueError, "no device acquisition timestamp"):
            datasync.series_from_run(run_engine((evidence,)), "DUT")

    def test_missing_or_invalid_reference_device_time_is_rejected(self):
        cases = (
            MappingProxyType({
                "t": 4_102_444_800.0,
                "ref": 20.0,
                "duts": MappingProxyType({"DUT": 20.1}),
            }),
            sample({"REF": "2026-08-05T12:00:01Z", "DUT": "2026:08:05 12:00:01 000"}),
            sample({"REF": "2026:13:99 25:61:61 999", "DUT": "2026:08:05 12:00:01 000"}),
        )

        for evidence in cases:
            with self.subTest(timestamp=evidence.get("device_timestamp")):
                with self.assertRaisesRegex(
                        ValueError, "device acquisition timestamp"):
                    datasync.series_from_run(run_engine((evidence,)), "REF")

    def test_non_finite_run_values_are_rejected(self):
        for value in (math.inf, -math.inf, math.nan, "NaN"):
            with self.subTest(value=value):
                evidence = sample(
                    {"REF": "2026:08:05 12:00:01 000", "DUT": "2026:08:05 12:00:01 000"},
                    ref=value,
                )
                with self.assertRaisesRegex(ValueError, "non-finite device value"):
                    datasync.series_from_run(run_engine((evidence,)), "REF")


class ExplicitUnitTests(unittest.TestCase):
    def test_load_rejects_invalid_source_unit_when_conversion_is_disabled(self):
        with patch.object(datasync, "analyze_file", return_value=analyzed_file()):
            with self.assertRaisesRegex(ValueError, "temperature unit"):
                datasync.load_any(
                    "logger.csv", unit="Rankine", convert_to=None,
                    log_fn=lambda _message: None,
                )

    def test_load_rejects_invalid_equal_source_and_display_units(self):
        with patch.object(datasync, "analyze_file", return_value=analyzed_file()):
            with self.assertRaisesRegex(ValueError, "temperature unit"):
                datasync.load_any(
                    "logger.csv", unit="Rankine", convert_to="Rankine",
                    log_fn=lambda _message: None,
                )

    def test_load_rejects_non_celsius_display_target_even_for_celsius_source(self):
        with patch.object(datasync, "analyze_file", return_value=analyzed_file()):
            with self.assertRaisesRegex(ValueError, "conversion to Celsius"):
                datasync.load_any(
                    "logger.csv", unit="C", convert_to="F",
                    log_fn=lambda _message: None,
                )

    def test_load_normalizes_and_records_valid_source_and_display_units(self):
        with patch.object(
                datasync, "analyze_file", return_value=analyzed_file("Temp F")):
            frame = datasync.load_any(
                "logger.csv", unit=" f ", convert_to=" c ",
                log_fn=lambda _message: None,
            )

        self.assertAlmostEqual(frame.loc[0, "_value"], -6.666666666666667)
        self.assertEqual(frame.attrs["source_unit"], "F")
        self.assertEqual(frame.attrs["display_unit"], "C")

    def test_load_without_conversion_still_records_validated_source_unit(self):
        with patch.object(datasync, "analyze_file", return_value=analyzed_file()):
            frame = datasync.load_any(
                "logger.csv", unit=" c ", convert_to=False,
                log_fn=lambda _message: None,
            )

        self.assertEqual(frame.attrs["source_unit"], "C")
        self.assertEqual(frame.attrs["display_unit"], "C")

    def test_chart_rejects_non_celsius_or_unstated_frames(self):
        for display_unit in ("F", "K", None):
            with self.subTest(display_unit=display_unit):
                frame = pd.DataFrame({
                    "_abs_time": pd.to_datetime(["2026-08-05 12:00:00"]),
                    "_value": [20.0],
                })
                if display_unit is not None:
                    frame.attrs["display_unit"] = display_unit
                with self.assertRaisesRegex(ValueError, "Celsius-labelled chart"):
                    datasync.build_chart(frame, [], [], "unused.html")


if __name__ == "__main__":
    unittest.main()
