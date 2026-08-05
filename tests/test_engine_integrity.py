import math
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, FakeRegistry, MinimalAdt

bootstrap_calsuite()

from calsuite.adt286 import Reading
from calsuite.engine import (
    RunEngine,
    SetPointResult,
    STATE_ERROR,
    STATE_RUNNING,
    default_profile,
    validate_profile,
)


def valid_profile():
    profile = default_profile("Integrity profile")
    profile.update({
        "heat_source": "Test Well",
        "reference_channel": "REF",
        "dut_channels": ["DUT"],
        "setpoints": [0.0],
        "sample_count": 1,
        "sample_interval": 0.0,
    })
    return profile


def complete_result(*, expected=1, stable=True, confirmed=True,
                    ref=20.0, dut=20.01, tolerance=0.05):
    result = SetPointResult(
        20.0, "°C", tolerance=tolerance, expected_samples=expected,
    )
    result.setpoint_confirmed = confirmed
    result.source_checks_valid = confirmed is True
    result.stable = stable
    result.samples = [{"ref": ref, "duts": {"DUT": dut}}]
    result.summarise(["DUT"])
    return result


class ProfileValidationTests(unittest.TestCase):
    def setUp(self):
        self.heat = FakeHeatSource()
        self.channels = ["REF", "DUT"]

    def validate(self, profile):
        return validate_profile(
            profile, self.heat, self.channels,
            poll_interval=1.0, readout_unit="°C",
        )

    def test_known_good_profile_has_no_problems(self):
        self.assertEqual(self.validate(valid_profile()), [])

    def test_negative_nan_and_malformed_numeric_settings_are_rejected(self):
        cases = {
            "sample_count_negative": ("sample_count", -1),
            "sample_count_nan": ("sample_count", math.nan),
            "sample_count_text": ("sample_count", "not-a-number"),
            "stability_band_negative": ("stability_band", -0.1),
            "stability_band_nan": ("stability_band", math.nan),
            "stability_window_negative": ("stability_window", -1),
            "stability_window_nan": ("stability_window", math.nan),
            "max_wait_negative": ("max_wait", -1),
            "max_wait_nan": ("max_wait", math.nan),
            "sample_interval_negative": ("sample_interval", -1),
            "sample_interval_nan": ("sample_interval", math.nan),
            "soak_negative": ("soak_seconds", -1),
            "soak_nan": ("soak_seconds", math.nan),
            "tolerance_nan": ("tolerance", math.nan),
        }
        for label, (field, value) in cases.items():
            with self.subTest(case=label):
                profile = valid_profile()
                profile[field] = value
                self.assertTrue(self.validate(profile))

    def test_nonfinite_setpoint_and_invalid_timeout_choice_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(setpoint=value):
                profile = valid_profile()
                profile["setpoints"] = [value]
                self.assertTrue(self.validate(profile))

        profile = valid_profile()
        profile["on_timeout"] = "silently-ignore"
        self.assertTrue(self.validate(profile))

    def test_malformed_persisted_profile_shapes_report_problems_not_exceptions(self):
        cases = {
            "name": None,
            "reference_channel": 17,
            "dut_channels": None,
            "setpoints": None,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                profile = valid_profile()
                profile[field] = value
                problems = self.validate(profile)
                self.assertTrue(problems)

        profile = valid_profile()
        profile["tolerance_mode"] = "per_point"
        profile["tolerances"] = 0.05
        self.assertTrue(self.validate(profile))

    def test_unit_mismatches_are_rejected(self):
        profile = valid_profile()
        problems = validate_profile(
            profile, self.heat, self.channels,
            poll_interval=1.0, readout_unit="°F",
        )
        self.assertTrue(any("Unit mismatch" in item for item in problems))

        heat = FakeHeatSource(unit="°C", reported_unit="°F")
        problems = validate_profile(
            profile, heat, self.channels,
            poll_interval=1.0, readout_unit="°C",
        )
        self.assertTrue(any("configured range" in item for item in problems))


class StatisticsAndVerdictTests(unittest.TestCase):
    def test_sample_sd_is_none_for_one_observation(self):
        stats = SetPointResult._stats([12.345])
        self.assertEqual(stats["n"], 1)
        self.assertIsNone(stats["sd"])

    def test_complete_stable_confirmed_result_can_pass(self):
        self.assertEqual(complete_result().verdict, "pass")

    def test_unconfirmed_unstable_and_incomplete_results_are_invalid(self):
        cases = {
            "unconfirmed": complete_result(confirmed=False),
            "unknown_confirmation": complete_result(confirmed=None),
            "unstable": complete_result(stable=False),
            "incomplete": complete_result(expected=2),
        }
        for label, result in cases.items():
            with self.subTest(case=label):
                self.assertEqual(result.verdict, "invalid")
                self.assertTrue(result.quality_issues)


class EngineEvidenceTests(unittest.TestCase):
    def test_profile_is_deep_copied_for_run_lifetime(self):
        original = valid_profile()
        original["extra"] = {"nested": ["original"]}
        engine = RunEngine("run", original, None, None, None)

        original["dut_channels"].append("OTHER")
        original["setpoints"][0] = 99.0
        original["extra"]["nested"].append("changed")

        self.assertEqual(engine.profile["dut_channels"], ["DUT"])
        self.assertEqual(engine.profile["setpoints"], [0.0])
        self.assertEqual(engine.profile["extra"], {"nested": ["original"]})

    def test_start_pins_device_evidence_and_nested_channel_configuration(self):
        profile = valid_profile()
        heat = FakeHeatSource()
        adt = MinimalAdt()
        registry = FakeRegistry()
        engine = RunEngine("run", profile, heat, adt, registry)
        fake_thread = Mock()
        fake_thread.is_alive.return_value = False

        with patch("calsuite.engine.threading.Thread", return_value=fake_thread):
            engine.start()

        fake_thread.start.assert_called_once_with()
        self.assertIsInstance(engine.evidence, MappingProxyType)
        self.assertEqual(engine.evidence["readout_identity"],
                         "ADDITEL,ADT286,SERIAL,1.0")
        self.assertEqual(
            engine.evidence["channel_configuration"]["REF"]["serial"],
            "REF-SERIAL",
        )

        adt.idn = "MUTATED"
        adt.channel_info["REF"]["serial"] = "MUTATED"
        heat.name = "MUTATED"

        self.assertEqual(engine.evidence["readout_identity"],
                         "ADDITEL,ADT286,SERIAL,1.0")
        self.assertEqual(engine.evidence["heat_source"], "Test Well")
        self.assertEqual(
            engine.evidence["channel_configuration"]["REF"]["serial"],
            "REF-SERIAL",
        )
        with self.assertRaises(TypeError):
            engine.evidence["readout_unit"] = "°F"
        with self.assertRaises(TypeError):
            engine.evidence["channel_configuration"]["REF"]["serial"] = "X"

    def test_fresh_sample_frame_waits_for_complete_same_cycle_data(self):
        class ScriptedAdt(MinimalAdt):
            def __init__(self):
                super().__init__()
                self.references = iter([
                    Reading("REF", 20.0, "°C", cycle=1,
                            raw_temperature="20.0"),
                    Reading("REF", 20.0, "°C", cycle=2,
                            raw_temperature="20.0"),
                ])

            def latest(self, channel):
                return next(self.references)

            def snapshot(self, channels, cycle=None):
                if cycle == 1:
                    return {
                        "REF": Reading("REF", 20.0, "°C", cycle=1,
                                       raw_temperature="20.0"),
                        "DUT": None,
                    }
                return {
                    "REF": Reading("REF", 20.0, "°C", cycle=2,
                                   raw_temperature="20.0"),
                    "DUT": Reading("DUT", 20.1, "°C", cycle=2,
                                   raw_temperature="20.1"),
                }

        engine = RunEngine(
            "run", valid_profile(), FakeHeatSource(), ScriptedAdt(), FakeRegistry(),
        )
        engine.measurement_unit = "°C"
        engine._stop.wait = Mock(return_value=False)

        cycle, frame = engine._fresh_sample_frame(0)

        self.assertEqual(cycle, 2)
        self.assertEqual({reading.cycle for reading in frame.values()}, {2})

    def test_complete_raw_sample_frame_is_exact_and_immutable(self):
        adt = MinimalAdt()
        engine = RunEngine(
            "run", valid_profile(), FakeHeatSource(), adt, FakeRegistry(),
        )
        engine.measurement_unit = "°C"
        reference = Reading(
            "REF", 20.123456789, "°C", cycle=7,
            timestamp=1_700_000_000.123456,
            raw_temperature="20.123456789012345678",
        )
        dut = Reading(
            "DUT", 20.223456789, "°C", cycle=7,
            timestamp=1_700_000_000.123456,
            raw_temperature="20.223456789012345678",
        )
        engine._fresh_sample_frame = Mock(
            return_value=(7, {"REF": reference, "DUT": dut})
        )
        captured_events = []
        engine.event_cb = captured_events.append
        result = SetPointResult(20.0, "°C", 0.05, expected_samples=1)

        self.assertTrue(engine._take_samples(result))
        sample = result.samples[0]

        self.assertIsInstance(sample, MappingProxyType)
        self.assertEqual(sample["t"], reference.timestamp)
        self.assertEqual(sample["cycle"], 7)
        self.assertEqual(sample["ref"], reference.temperature)
        self.assertEqual(sample["ref_raw"], reference.raw_temperature)
        self.assertEqual(sample["duts"]["DUT"], dut.temperature)
        self.assertEqual(sample["duts_raw"]["DUT"], dut.raw_temperature)
        self.assertEqual(sample["source"], reference.source)
        with self.assertRaises(TypeError):
            sample["ref"] = 99.0
        with self.assertRaises(TypeError):
            sample["duts"]["DUT"] = 99.0
        event_sample = next(
            event["sample"] for event in captured_events
            if event.get("kind") == "sample"
        )
        self.assertIs(event_sample, sample)

    def test_incomplete_frame_is_not_recorded(self):
        engine = RunEngine(
            "run", valid_profile(), FakeHeatSource(), MinimalAdt(), FakeRegistry(),
        )
        engine.measurement_unit = "°C"
        engine._fresh_sample_frame = Mock(return_value=(1, None))
        result = SetPointResult(20.0, "°C", 0.05, expected_samples=1)

        with self.assertRaises(RuntimeError):
            engine._take_samples(result)
        self.assertEqual(result.samples, [])


class SetpointConfirmationTests(unittest.TestCase):
    def test_rejected_setpoint_write_aborts_before_readback_or_sampling(self):
        heat = FakeHeatSource(confirm=(True, 20.0))
        heat.set_setpoint = Mock(return_value=False)
        heat.confirm_setpoint = Mock(return_value=(True, 20.0))
        engine = RunEngine(
            "run", valid_profile(), heat, MinimalAdt(), FakeRegistry(),
        )
        engine.measurement_unit = "°C"
        engine.state = STATE_RUNNING
        engine._stabilize = Mock(return_value=(True, 0.0, ""))
        engine._take_samples = Mock(return_value=True)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("did not confirm that the set-point command was sent",
                      engine.error)
        self.assertEqual(engine.results, ())
        heat.confirm_setpoint.assert_not_called()
        engine._stabilize.assert_not_called()
        engine._take_samples.assert_not_called()

    def test_failed_and_unavailable_setpoint_confirmation_abort_before_sampling(self):
        cases = {
            "failed": ((False, 25.0), "did not confirm"),
            "unavailable": ((None, None), "cannot confirm"),
        }
        for label, (confirmation, message) in cases.items():
            with self.subTest(case=label):
                heat = FakeHeatSource(confirm=confirmation)
                adt = MinimalAdt()
                registry = FakeRegistry()
                engine = RunEngine("run", valid_profile(), heat, adt, registry)
                engine.measurement_unit = "°C"
                engine.state = STATE_RUNNING
                engine._stabilize = Mock(return_value=(True, 0.0, ""))
                engine._take_samples = Mock(return_value=True)

                engine._run()

                self.assertEqual(engine.state, STATE_ERROR)
                self.assertIn(message, engine.error)
                self.assertEqual(len(engine.results), 1)
                preserved = engine.results[0]
                self.assertEqual(preserved.verdict, "invalid")
                self.assertEqual(preserved.samples, ())
                self.assertEqual(len(preserved.source_checks), 1)
                engine._stabilize.assert_not_called()
                engine._take_samples.assert_not_called()


if __name__ == "__main__":
    unittest.main()
