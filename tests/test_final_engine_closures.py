"""Final regressions for run-state, verdict, and live-source safeguards."""

from types import MappingProxyType
import unittest
from unittest.mock import Mock

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, FakeRegistry, MinimalAdt
from tests.test_engine_integrity import valid_profile

bootstrap_calsuite()

from calsuite.adt286 import Reading
from calsuite.engine import (
    RunEngine,
    SetPointResult,
    STATE_DONE,
    STATE_ERROR,
    STATE_RUNNING,
)
from calsuite.ui import SuiteApp


DEG_C = "\N{DEGREE SIGN}C"


def valid_result(
        references, *, setpoint=20.0, band=0.25, proximity=1.0,
        expected_duts=("DUT",), summarised_duts=("DUT",)):
    result = SetPointResult(
        setpoint,
        DEG_C,
        tolerance=0.05,
        expected_samples=len(references),
        expected_dut_channels=expected_duts,
        sample_stability_band=band,
        setpoint_tolerance=proximity,
    )
    result.setpoint_confirmed = True
    result.source_checks_valid = True
    result.stable = True
    result.samples = [
        MappingProxyType({
            "ref": reference,
            "duts": MappingProxyType({
                channel: reference + 0.01 for channel in summarised_duts
            }),
        })
        for reference in references
    ]
    return result.summarise(list(summarised_duts))


class NumericListInputTests(unittest.TestCase):
    def test_whitespace_inside_numeric_token_is_rejected_not_concatenated(self):
        values, invalid = SuiteApp._parse_numeric_list("1 2,3")

        self.assertEqual(values, [3.0])
        self.assertEqual(invalid, ["1 2"])
        self.assertNotIn(12.0, values)


class CompletedRunStateTests(unittest.TestCase):
    def test_stop_on_completed_run_is_a_complete_no_op(self):
        engine = RunEngine("run", valid_profile(), None, None, None)
        engine.state = STATE_DONE
        engine.error = ""
        engine.finished_at = 1_700_000_000.0
        original_results = [object()]
        engine.results = original_results

        stopped = engine.stop("late stop must not alter evidence")

        self.assertFalse(stopped)
        self.assertEqual(engine.state, STATE_DONE)
        self.assertEqual(engine.error, "")
        self.assertEqual(engine.finished_at, 1_700_000_000.0)
        self.assertIs(engine.results, original_results)
        self.assertFalse(engine._stop.is_set())


class DerivedResultIntegrityTests(unittest.TestCase):
    def test_missing_expected_dut_is_invalid_and_all_stats_are_immutable(self):
        result = valid_result(
            (19.99, 20.01),
            expected_duts=("DUT1", "DUT2"),
            summarised_duts=("DUT1",),
        )

        self.assertEqual(result.verdict, "invalid")
        self.assertIn("DUT2 result missing", result.quality_issues)
        self.assertIsInstance(result.reference, MappingProxyType)
        self.assertIsInstance(result.duts, MappingProxyType)
        self.assertIsInstance(result.duts["DUT1"], MappingProxyType)
        with self.assertRaises(TypeError):
            result.reference["mean"] = 0.0
        with self.assertRaises(TypeError):
            result.duts["DUT1"] = {}
        with self.assertRaises(TypeError):
            result.duts["DUT1"]["error"] = 0.0

    def test_sampled_reference_mean_outside_setpoint_limit_is_invalid(self):
        result = valid_result(
            (21.0, 21.1), band=0.25, proximity=0.5,
        )

        self.assertEqual(result.verdict, "invalid")
        self.assertIn(
            "sampled reference mean outside set-point tolerance",
            result.quality_issues,
        )

    def test_sampled_reference_span_outside_stability_limit_is_invalid(self):
        result = valid_result(
            (19.9, 20.1), band=0.02, proximity=1.0,
        )

        self.assertEqual(result.verdict, "invalid")
        self.assertIn(
            "sampled reference span exceeds stability band",
            result.quality_issues,
        )


class SourceContinuityTests(unittest.TestCase):
    @staticmethod
    def _reading(channel, value, cycle):
        return Reading(
            channel,
            value,
            DEG_C,
            cycle=cycle,
            timestamp=1_700_000_000.0 + cycle,
            device_timestamp=f"2026:08:05 17:04:{cycle:02d} 123",
            raw_temperature=f"{value:.10f}",
        )

    def test_every_sample_reconfirms_source_and_pins_its_exact_reply(self):
        profile = valid_profile()
        profile.update({"setpoints": [20.0], "sample_count": 2,
                        "sample_interval": 0.0})
        heat = FakeHeatSource(
            unit=DEG_C, reported_unit=DEG_C, confirm=(True, 20.0),
        )
        heat.last_setpoint_readback_raw = "20.000000 C"
        heat.confirm_setpoint = Mock(return_value=(True, 20.0))
        engine = RunEngine(
            "run", profile, heat, MinimalAdt(unit=DEG_C), FakeRegistry(),
        )
        engine.measurement_unit = DEG_C
        engine._fresh_sample_frame = Mock(side_effect=[
            (1, {
                "REF": self._reading("REF", 20.00, 1),
                "DUT": self._reading("DUT", 20.01, 1),
            }),
            (2, {
                "REF": self._reading("REF", 20.02, 2),
                "DUT": self._reading("DUT", 20.03, 2),
            }),
        ])
        result = SetPointResult(
            20.0, DEG_C, 0.05, expected_samples=2,
            expected_dut_channels=("DUT",),
        )

        self.assertTrue(engine._take_samples(result))

        self.assertEqual(heat.confirm_setpoint.call_count, 2)
        self.assertEqual(len(result.source_checks), 2)
        self.assertTrue(all(
            isinstance(check, MappingProxyType)
            for check in result.source_checks
        ))
        for index, sample in enumerate(result.samples, 1):
            self.assertEqual(sample["source_setpoint"], 20.0)
            self.assertEqual(sample["source_setpoint_raw"], "20.000000 C")
            self.assertEqual(sample["source_setpoint_unit"], DEG_C)
            self.assertIn(f"before sample {index}",
                          result.source_checks[index - 1]["context"])

    def test_failed_post_sampling_source_check_invalidates_preserved_evidence(self):
        profile = valid_profile()
        profile.update({
            "setpoints": [20.0],
            "sample_count": 1,
            "sample_interval": 0.0,
            "enable_output": False,
            "disable_at_end": False,
        })
        heat = FakeHeatSource(
            unit=DEG_C, reported_unit=DEG_C, confirm=(True, 20.0),
        )
        replies = iter(((True, 20.0, "20.000000 C"),
                        (False, 21.0, "21.000000 C")))

        def confirm(_expected):
            ok, value, raw = next(replies)
            heat.last_setpoint_readback_raw = raw
            return ok, value

        heat.confirm_setpoint = Mock(side_effect=confirm)
        engine = RunEngine(
            "run", profile, heat, MinimalAdt(unit=DEG_C), FakeRegistry(),
        )
        engine.measurement_unit = DEG_C
        engine.state = STATE_RUNNING
        engine._stabilize = Mock(return_value=(True, 0.0, ""))

        def take_one(result):
            result.samples.append(MappingProxyType({
                "ref": 20.0,
                "duts": MappingProxyType({"DUT": 20.01}),
            }))
            return True

        engine._take_samples = Mock(side_effect=take_one)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("readback is 21.0", engine.error)
        self.assertEqual(heat.confirm_setpoint.call_count, 2)
        engine._take_samples.assert_called_once()
        self.assertEqual(len(engine.results), 1)
        result = engine.results[0]
        self.assertFalse(result.source_checks_valid)
        self.assertEqual(result.verdict, "invalid")
        self.assertEqual(len(result.source_checks), 2)
        self.assertIn("after sampling", result.source_checks[-1]["context"])
        self.assertEqual(result.source_checks[-1]["raw"], "21.000000 C")


if __name__ == "__main__":
    unittest.main()
