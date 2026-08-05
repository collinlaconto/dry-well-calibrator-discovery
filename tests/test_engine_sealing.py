"""Regression tests for immutable run evidence and live unit validation."""

from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, FakeRegistry, MinimalAdt
from tests.test_engine_integrity import complete_result, valid_profile

bootstrap_calsuite()

from calsuite.engine import RunEngine, STATE_ERROR, STATE_RUNNING


DEG_C = "\N{DEGREE SIGN}C"


class RefreshingHeatSource(FakeHeatSource):
    def __init__(self, *, failure=None):
        super().__init__(unit=DEG_C, reported_unit="")
        self.failure = failure
        self.refresh_calls = 0
        self.last_unit_reply = ""

    def refresh_reported_unit(self):
        self.refresh_calls += 1
        if self.failure is not None:
            raise self.failure
        self.reported_unit = DEG_C
        self.last_unit_reply = "C"
        return self.reported_unit


class LiveStartValidationTests(unittest.TestCase):
    def test_start_refreshes_source_unit_before_profile_validation(self):
        heat = RefreshingHeatSource()
        registry = FakeRegistry()
        engine = RunEngine(
            "run", valid_profile(), heat, MinimalAdt(unit=DEG_C), registry,
        )
        fake_thread = Mock()
        fake_thread.is_alive.return_value = False

        with patch("calsuite.engine.threading.Thread", return_value=fake_thread):
            engine.start()

        self.assertEqual(heat.refresh_calls, 1)
        self.assertEqual(engine.evidence["heat_source_reported_unit"], DEG_C)
        self.assertEqual(registry.claimed, [("run", ("REF", "DUT"))])

    def test_failed_live_unit_refresh_does_not_claim_devices(self):
        heat = RefreshingHeatSource(failure=OSError("query failed"))
        registry = FakeRegistry()
        engine = RunEngine(
            "run", valid_profile(), heat, MinimalAdt(unit=DEG_C), registry,
        )

        with self.assertRaisesRegex(RuntimeError, "current temperature unit"):
            engine.start()

        self.assertEqual(registry.claimed, [])

    def test_boolean_profile_values_are_not_accepted_as_numbers(self):
        profile = valid_profile()
        profile["setpoints"] = [True]
        engine = RunEngine(
            "run", profile, FakeHeatSource(), MinimalAdt(), FakeRegistry(),
        )

        with self.assertRaisesRegex(ValueError, "finite number"):
            engine.start()


class EvidenceSealingTests(unittest.TestCase):
    def test_validated_profile_and_nested_settings_are_immutable(self):
        profile = valid_profile()
        profile["extra"] = {"nested": ["device sourced"]}
        engine = RunEngine(
            "run", profile, FakeHeatSource(), MinimalAdt(), FakeRegistry(),
        )
        fake_thread = Mock()
        fake_thread.is_alive.return_value = False

        with patch("calsuite.engine.threading.Thread", return_value=fake_thread):
            engine.start()

        self.assertIsInstance(engine.profile, MappingProxyType)
        self.assertEqual(engine.profile["dut_channels"], ("DUT",))
        self.assertEqual(engine.profile["extra"]["nested"], ("device sourced",))
        with self.assertRaises(TypeError):
            engine.profile["name"] = "relabeled"
        with self.assertRaises(TypeError):
            engine.profile["extra"]["nested"] += ("mutated",)

    def test_summarised_result_and_raw_samples_are_recursively_sealed(self):
        result = complete_result()

        with self.assertRaises(AttributeError):
            result.stable = False
        with self.assertRaises(AttributeError):
            result._sealed = False
        with self.assertRaises(TypeError):
            result.samples[0]["ref"] = 999.0
        with self.assertRaises(TypeError):
            result.samples[0]["duts"]["DUT"] = 999.0

    def test_finalized_engine_results_are_a_tuple_even_after_error(self):
        heat = FakeHeatSource(confirm=(False, 20.0))
        engine = RunEngine(
            "run", valid_profile(), heat, MinimalAdt(), FakeRegistry(),
        )
        engine.measurement_unit = DEG_C
        engine.state = STATE_RUNNING
        engine._stabilize = Mock(return_value=(True, 0.0, ""))
        engine._take_samples = Mock(return_value=True)

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIsInstance(engine.results, tuple)
        self.assertEqual(len(engine.results), 1)
        self.assertEqual(engine.results[0].verdict, "invalid")
        self.assertEqual(len(engine.results[0].source_checks), 1)
        for field, replacement in (
                ("profile", {}), ("evidence", {}), ("results", ()),
                ("state", "complete"), ("error", "relabeled")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(AttributeError, "finalized evidence"):
                    setattr(engine, field, replacement)
        with self.assertRaises(AttributeError):
            engine._sealed_run = False
        with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
            engine.start()


if __name__ == "__main__":
    unittest.main()
