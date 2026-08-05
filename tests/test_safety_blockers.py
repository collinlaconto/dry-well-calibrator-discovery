"""Regression tests for device-source and evidence-integrity blockers."""

import tempfile
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, FakeRegistry, MinimalAdt
from tests.test_adt286_integrity import scan_group
from tests.test_engine_integrity import valid_profile
from tests.test_export_integrity import export_fixture

bootstrap_calsuite()

from calsuite.adt286 import Adt286, Reading, parse_scan_data
from calsuite.datasync import series_from_run
from calsuite.engine import RunEngine, SetPointResult, STATE_ERROR, STATE_RUNNING
from calsuite.export import export_run
from calsuite.heatsource import HeatSource


DEG_C = "\N{DEGREE SIGN}C"
DEG_F = "\N{DEGREE SIGN}F"


class HeatSourceUnitFreshnessTests(unittest.TestCase):
    def test_unitless_current_reply_clears_cached_unit_evidence(self):
        source = HeatSource({
            "name": "Well", "range_unit": DEG_C,
            "range_min": -20, "range_max": 150,
        })
        source.reported_unit = DEG_C
        source.last_unit_reply = "1001"

        source._capture_unit("20.0000")

        self.assertEqual(source.reported_unit, "")
        self.assertEqual(source.last_unit_reply, "")


def timestamped_group(channel, value, device_timestamp, placement="suffix"):
    """Return a documented format-1 scan group with its device timestamp."""
    measurement = scan_group(channel, value)
    if placement == "prefix":
        return f"{device_timestamp},{measurement}"
    return f"{measurement},{device_timestamp}"


def timestamped_payload(device_timestamp, ref="20.0", dut="20.1"):
    groups = (
        timestamped_group("REF", ref, device_timestamp),
        timestamped_group("DUT", dut, device_timestamp),
    )
    return '"' + ";".join(groups) + ';"'


class SequenceLink:
    is_open = True

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.queries = []

    def query(self, command):
        self.queries.append(command)
        return self.payloads.pop(0)

    def write(self, command):
        return None


class DeviceTimestampTests(unittest.TestCase):
    def test_format_one_timestamp_is_retained_before_or_after_scan_fields(self):
        stamp = "2026:08:05 17:04:03 123"

        for placement in ("prefix", "suffix"):
            with self.subTest(placement=placement):
                parsed = parse_scan_data(
                    timestamped_group("REF", "20.125", stamp, placement)
                )["REF"]
                self.assertEqual(parsed["temperature"], 20.125)
                self.assertEqual(parsed["raw_temperature"], "20.125")
                self.assertEqual(parsed["device_timestamp"], stamp)

    def test_repeated_device_timestamp_does_not_create_a_new_scan_cycle(self):
        first_stamp = "2026:08:05 17:04:03 123"
        next_stamp = "2026:08:05 17:04:04 123"
        link = SequenceLink([
            timestamped_payload(first_stamp, "20.0", "20.1"),
            timestamped_payload(first_stamp, "99.0", "99.1"),
            timestamped_payload(next_stamp, "20.2", "20.3"),
        ])
        adt = Adt286()
        adt.link = link
        adt._subs = {"run": ["REF", "DUT"]}

        with patch("calsuite.adt286.time.time",
                   return_value=1_700_000_000.125):
            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 2)
            first = adt.latest("REF")
            self.assertEqual(adt.cycle, 1)
            self.assertEqual(first.device_timestamp, first_stamp)
            self.assertEqual(first.timestamp, 1_700_000_000.125)

            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 0)
            self.assertEqual(adt.cycle, 1)

            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 2)
            self.assertEqual(adt.cycle, 2)
            self.assertEqual(adt.latest("REF").device_timestamp, next_stamp)

        self.assertEqual(
            link.queries,
            ["SCAN:DATA:Last? 1", "SCAN:DATA:Last? 1", "SCAN:DATA:Last? 1"],
        )

    def test_reply_without_authoritative_device_timestamp_is_not_published(self):
        link = SequenceLink([
            scan_group("REF", "20.0") + ";" + scan_group("DUT", "20.1")
        ])
        adt = Adt286()
        adt.link = link
        adt._subs = {"run": ["REF", "DUT"]}
        adt._last_poll_started = -1e9

        self.assertEqual(adt.poll_once(), 0)
        self.assertEqual(adt.cycle, 0)
        self.assertIsNone(adt.latest("REF"))
        self.assertIsNone(adt.latest("DUT"))
        self.assertEqual(link.queries, ["SCAN:DATA:Last? 1"])

    def test_raw_sample_preserves_host_and_device_timestamps(self):
        stamp = "2026:08:05 17:04:03 123"
        host_time = 1_700_000_000.123456
        reference = Reading(
            "REF", 20.0, DEG_C, cycle=7, timestamp=host_time,
            device_timestamp=stamp, raw_temperature="20.0000000000",
        )
        dut = Reading(
            "DUT", 20.1, DEG_C, cycle=7, timestamp=host_time,
            device_timestamp=stamp, raw_temperature="20.1000000000",
        )
        engine = RunEngine(
            "run", valid_profile(), FakeHeatSource(), MinimalAdt(),
            FakeRegistry(),
        )
        engine.measurement_unit = DEG_C
        engine._fresh_sample_frame = Mock(
            return_value=(7, {"REF": reference, "DUT": dut})
        )
        result = SetPointResult(20.0, DEG_C, 0.05, expected_samples=1)

        self.assertTrue(engine._take_samples(result))

        self.assertEqual(result.samples[0]["t"], host_time)
        self.assertEqual(result.samples[0]["device_timestamp"], stamp)


class DerivedSeriesIntegrityTests(unittest.TestCase):
    def test_pinned_fahrenheit_and_kelvin_runs_convert_only_the_chart_copy(self):
        cases = (
            (DEG_F, (32.0, 212.0), (0.0, 100.0)),
            ("K", (273.15, 373.15), (0.0, 100.0)),
        )

        for pinned_unit, raw_values, expected_values in cases:
            with self.subTest(pinned_unit=pinned_unit):
                samples = tuple(
                    MappingProxyType({
                        "t": 1_700_000_000.0 + index,
                        "device_timestamp": (
                            f"2026:08:05 17:04:0{index} 000"),
                        "device_timestamps": MappingProxyType({
                            "REF": f"2026:08:05 17:04:0{index} 000",
                            "DUT": f"2026:08:05 17:04:0{index} 000",
                        }),
                        "ref": value,
                        "ref_raw": f"{value:.12f}",
                        "duts": MappingProxyType({"DUT": value + 0.25}),
                        "duts_raw": MappingProxyType({
                            "DUT": f"{value + 0.25:.12f}",
                        }),
                    })
                    for index, value in enumerate(raw_values)
                )
                original_samples = tuple(samples)
                original_duts = tuple(sample["duts"] for sample in samples)
                original_values = tuple(sample["ref"] for sample in samples)
                original_tokens = tuple(sample["ref_raw"] for sample in samples)
                engine = SimpleNamespace(
                    profile={
                        "reference_channel": "REF",
                        "dut_channels": ["DUT"],
                    },
                    evidence=MappingProxyType({
                        "readout_unit": pinned_unit,
                    }),
                    results=[SimpleNamespace(samples=samples)],
                )

                frame = series_from_run(engine, channel="REF")

                for actual, expected in zip(frame["_value"], expected_values):
                    self.assertAlmostEqual(actual, expected, places=10)
                self.assertEqual(frame.attrs["source_unit"],
                                 "F" if pinned_unit == DEG_F else "K")
                self.assertEqual(frame.attrs["display_unit"], "C")
                self.assertEqual(tuple(sample["ref"] for sample in samples),
                                 original_values)
                self.assertEqual(tuple(sample["ref_raw"] for sample in samples),
                                 original_tokens)
                for index, sample in enumerate(samples):
                    self.assertIs(sample, original_samples[index])
                    self.assertIs(sample["duts"], original_duts[index])
                    with self.assertRaises(TypeError):
                        sample["ref"] = 999.0


class ActiveExportTests(unittest.TestCase):
    def test_active_run_cannot_export_or_create_partial_files(self):
        engine, adt = export_fixture()
        engine.state = STATE_RUNNING
        engine.is_active = True

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "still running"):
                export_run(engine, adt, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])


class SequencedUnitHeat(FakeHeatSource):
    def __init__(self, replies):
        super().__init__(
            unit=DEG_C, reported_unit=DEG_C, confirm=(True, 20.0),
        )
        self._unit_replies = list(replies)
        self.refresh_calls = 0

    def refresh_reported_unit(self):
        self.refresh_calls += 1
        if self._unit_replies:
            self.reported_unit = self._unit_replies.pop(0)
        return self.reported_unit


def unit_guard_engine(heat):
    profile = valid_profile()
    profile.update({
        "setpoints": [20.0],
        "enable_output": False,
        "disable_at_end": False,
    })
    engine = RunEngine(
        "run", profile, heat, MinimalAdt(unit=DEG_C), FakeRegistry(),
    )
    engine.measurement_unit = DEG_C
    engine.state = STATE_RUNNING
    engine._stabilize = Mock(return_value=(True, 0.0, ""))
    engine._take_samples = Mock(return_value=True)
    return engine


class LiveSourceUnitTests(unittest.TestCase):
    def test_unknown_live_unit_aborts_before_any_setpoint_or_sample(self):
        heat = SequencedUnitHeat([""])
        engine = unit_guard_engine(heat)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("did not report its temperature unit", engine.error)
        self.assertEqual(heat.refresh_calls, 1)
        self.assertEqual(heat.setpoints, [])
        self.assertEqual(engine.results, ())
        engine._stabilize.assert_not_called()
        engine._take_samples.assert_not_called()

    def test_unit_change_before_setpoint_write_aborts_without_commanding(self):
        heat = SequencedUnitHeat([DEG_C, DEG_F])
        engine = unit_guard_engine(heat)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("unit mismatch", engine.error.lower())
        self.assertEqual(heat.refresh_calls, 2)
        self.assertEqual(heat.setpoints, [])
        self.assertEqual(engine.results, ())
        engine._stabilize.assert_not_called()
        engine._take_samples.assert_not_called()

    def test_unit_change_after_readback_aborts_before_stability_or_sampling(self):
        heat = SequencedUnitHeat([DEG_C, DEG_C, DEG_F])
        heat.confirm_setpoint = Mock(return_value=(True, 20.0))
        engine = unit_guard_engine(heat)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("unit mismatch", engine.error.lower())
        self.assertEqual(heat.refresh_calls, 3)
        self.assertEqual(len(heat.setpoints), 1)
        heat.confirm_setpoint.assert_called_once_with(20.0)
        self.assertEqual(len(engine.results), 1)
        preserved = engine.results[0]
        self.assertEqual(preserved.verdict, "invalid")
        self.assertEqual(preserved.samples, ())
        self.assertEqual(len(preserved.source_checks), 1)
        self.assertEqual(preserved.source_checks[0]["readback_unit"], DEG_C)
        self.assertEqual(preserved.source_checks[0]["unit"], DEG_F)
        engine._stabilize.assert_not_called()
        engine._take_samples.assert_not_called()


if __name__ == "__main__":
    unittest.main()
