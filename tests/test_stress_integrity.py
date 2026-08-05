import math
import random
import statistics
import threading
from types import MappingProxyType
import unittest

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, FakeRegistry, MinimalAdt

bootstrap_calsuite()

from calsuite.adt286 import Reading, parse_scan_data
from calsuite.engine import (
    ChannelConflict,
    ChannelRegistry,
    RunEngine,
    SetPointResult,
    STATE_ERROR,
    STATE_RUNNING,
)
from tests.test_adt286_integrity import scan_group
from tests.test_engine_integrity import valid_profile


class ParserFuzzTests(unittest.TestCase):
    def test_seeded_malformed_payload_fuzz_never_returns_nonfinite_numbers(self):
        rng = random.Random(0x286CA1)
        alphabet = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789,+-.;:\" 'eE_\x00"
        )
        payloads = [
            "",
            ";",
            ",,,,,",
            "REF,1281,-999,1,2,1001,1,20",
            "REF,1281,999999999999999999999999,1,2,1001,1,20",
            "REF,1281,1,1,1,1001,1,1e309",
        ]
        for _ in range(750):
            size = rng.randrange(0, 180)
            payloads.append("".join(rng.choice(alphabet) for _ in range(size)))

        for index, payload in enumerate(payloads):
            with self.subTest(case=index):
                parsed = parse_scan_data(payload)
                self.assertIsInstance(parsed, dict)
                for reading in parsed.values():
                    for field in ("temperature", "electrical"):
                        value = reading[field]
                        self.assertTrue(value is None or math.isfinite(value))

    def test_all_float_nonfinite_spellings_are_unusable_and_auditable(self):
        tokens = (
            "nan", "NaN", "+nan", "-nan", "inf", "+inf", "-inf",
            "Infinity", "+Infinity", "-Infinity", "1e309", "-1e309",
        )
        for token in tokens:
            with self.subTest(token=token):
                parsed = parse_scan_data(scan_group("REF", token))["REF"]
                self.assertIsNone(parsed["temperature"])
                self.assertEqual(parsed["raw_temperature"], token)

    def test_unknown_unit_is_not_inferred_but_raw_value_is_retained(self):
        parsed = parse_scan_data(
            scan_group("REF", "123.456000000000000001", unit_id=7777)
        )["REF"]

        self.assertIsNone(parsed["unit"])
        self.assertIsNone(parsed["temperature"])
        self.assertEqual(parsed["raw_temperature"], "123.456000000000000001")


class HighVolumeStatisticsTests(unittest.TestCase):
    def test_ten_thousand_immutable_samples_are_summarised_without_mutation(self):
        count = 10_000
        samples = []
        expected_reference = []
        expected_dut = []
        for index in range(count):
            reference = 20.0 + (index % 10) * 0.001
            dut = reference + 0.01
            expected_reference.append(reference)
            expected_dut.append(dut)
            samples.append(MappingProxyType({
                "t": 1_700_000_000.0 + index / 1000.0,
                "cycle": index + 1,
                "source": "ADT286 SCAN:DATA:Last?",
                "ref": reference,
                "ref_raw": format(reference, ".17g"),
                "duts": MappingProxyType({"DUT": dut}),
                "duts_raw": MappingProxyType({"DUT": format(dut, ".17g")}),
                "units": MappingProxyType({"REF": "°C", "DUT": "°C"}),
            }))

        first = samples[0]
        last = samples[-1]
        first_raw = first["ref_raw"]
        last_raw = last["duts_raw"]["DUT"]
        result = SetPointResult(
            20.0, "°C", tolerance=0.05, expected_samples=count,
        )
        result.setpoint_confirmed = True
        result.source_checks_valid = True
        result.stable = True
        result.samples = samples

        result.summarise(["DUT"])

        self.assertIsInstance(result.samples, tuple)
        self.assertEqual(len(result.samples), count)
        self.assertEqual(result.samples[0], first)
        self.assertEqual(result.samples[-1], last)
        self.assertEqual(result.samples[0]["ref_raw"], first_raw)
        self.assertEqual(result.samples[-1]["duts_raw"]["DUT"], last_raw)
        self.assertEqual(result.reference["n"], count)
        self.assertEqual(result.duts["DUT"]["n"], count)
        self.assertAlmostEqual(
            result.reference["mean"], statistics.fmean(expected_reference), places=14,
        )
        self.assertAlmostEqual(
            result.reference["sd"], statistics.stdev(expected_reference), places=14,
        )
        self.assertAlmostEqual(
            result.duts["DUT"]["mean"], statistics.fmean(expected_dut), places=14,
        )
        self.assertAlmostEqual(result.duts["DUT"]["error"], 0.01, places=12)
        self.assertEqual(result.verdict, "pass")
        with self.assertRaises(TypeError):
            result.samples[0]["ref"] = 999.0
        with self.assertRaises(TypeError):
            result.samples[-1]["duts"]["DUT"] = 999.0


class ChannelRegistryStressTests(unittest.TestCase):
    def test_concurrent_claims_allow_exactly_one_owner_per_channel(self):
        workers = 48
        registry = ChannelRegistry()
        barrier = threading.Barrier(workers)
        outcomes = []
        outcome_lock = threading.Lock()

        def contend(index):
            barrier.wait()
            try:
                registry.claim(f"run-{index}", ["REF"])
                outcome = "claimed"
            except ChannelConflict:
                outcome = "conflict"
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=contend, args=(index,))
                   for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("conflict"), workers - 1)
        self.assertIn(registry.owner("REF"), {f"run-{i}" for i in range(workers)})
        self.assertEqual(registry.in_use(), {"REF": registry.owner("REF")})


class ExplicitEvidenceRequirementsTests(unittest.TestCase):
    def test_missing_readout_unit_is_rejected_instead_of_inferred(self):
        profile = valid_profile()
        heat = FakeHeatSource(unit="°C", reported_unit="")

        from calsuite.engine import validate_profile

        problems = validate_profile(
            profile, heat, ["REF", "DUT"],
            poll_interval=1.0, readout_unit="",
        )

        self.assertTrue(any("did not report" in problem for problem in problems))

    def test_missing_heat_source_read_and_write_commands_are_rejected(self):
        from calsuite.engine import validate_profile

        for missing, expected in (
            ("sp_write", "write command"),
            ("sp_read", "readback command"),
        ):
            with self.subTest(missing=missing):
                heat = FakeHeatSource()
                heat.profile[missing] = ""
                problems = validate_profile(
                    valid_profile(), heat, ["REF", "DUT"],
                    poll_interval=1.0, readout_unit="°C",
                )
                self.assertTrue(any(expected in problem for problem in problems))

    def test_missing_both_heat_source_commands_reports_both_requirements(self):
        from calsuite.engine import validate_profile

        heat = FakeHeatSource()
        heat.profile.pop("sp_write")
        heat.profile.pop("sp_read")

        problems = validate_profile(
            valid_profile(), heat, ["REF", "DUT"],
            poll_interval=1.0, readout_unit="°C",
        )

        self.assertTrue(any("write command" in problem for problem in problems))
        self.assertTrue(any("readback command" in problem for problem in problems))


class PartialResultPreservationTests(unittest.TestCase):
    @staticmethod
    def sample(cycle, setpoint):
        return MappingProxyType({
            "t": 1_700_000_000.0 + cycle,
            "cycle": cycle,
            "source": "ADT286 SCAN:DATA:Last?",
            "ref": setpoint,
            "ref_raw": f"{setpoint:.6f}",
            "duts": MappingProxyType({"DUT": setpoint + 0.01}),
            "duts_raw": MappingProxyType({"DUT": f"{setpoint + 0.01:.6f}"}),
            "units": MappingProxyType({"REF": "°C", "DUT": "°C"}),
        })

    @staticmethod
    def stability_sample(cycle, setpoint):
        return MappingProxyType({
            "t": 1_700_000_000.0 + cycle,
            "cycle": cycle,
            "source": "ADT286 SCAN:DATA:Last?",
            "ref": setpoint,
            "ref_raw": f"{setpoint:.9f}",
            "unit": "°C",
        })

    def test_completed_point_and_interrupted_point_evidence_are_both_preserved(self):
        profile = valid_profile()
        profile["setpoints"] = [0.0, 50.0]
        profile["enable_output"] = False
        profile["disable_at_end"] = False
        heat = FakeHeatSource(confirm=(True, 0.0))
        adt = MinimalAdt()
        registry = FakeRegistry()
        events = []
        engine = RunEngine("run", profile, heat, adt, registry, events.append)
        engine.measurement_unit = "°C"
        engine.state = STATE_RUNNING
        cycles = iter((1, 2))

        def stabilize(setpoint, result):
            cycle = next(cycles)
            result.stability_samples.append(
                self.stability_sample(cycle, setpoint)
            )
            return True, 2.0, ""

        sampling_calls = 0

        def take_samples(result):
            nonlocal sampling_calls
            sampling_calls += 1
            if sampling_calls == 1:
                result.samples.append(self.sample(10, result.setpoint))
                return True
            raise RuntimeError("simulated second-point acquisition failure")

        engine._stabilize = stabilize
        engine._take_samples = take_samples

        engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("second-point acquisition failure", engine.error)
        self.assertEqual(len(engine.results), 2)
        completed, interrupted = engine.results
        self.assertEqual(completed.setpoint, 0.0)
        self.assertEqual(completed.verdict, "pass")
        self.assertEqual(interrupted.setpoint, 50.0)
        self.assertEqual(interrupted.verdict, "invalid")
        self.assertEqual(interrupted.reference["n"], 0)
        self.assertEqual(len(interrupted.stability_samples), 1)
        self.assertEqual(interrupted.stability_samples[0]["ref_raw"],
                         "50.000000000")
        self.assertIn("point interrupted by acquisition error", interrupted.note)
        result_events = [event for event in events if event.get("kind") == "result"]
        self.assertEqual(len(result_events), 2)


if __name__ == "__main__":
    unittest.main()
