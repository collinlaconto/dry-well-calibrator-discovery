"""Regressions for exact heat-source reply provenance and preservation."""

import csv
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeRegistry, MinimalAdt
from tests.test_engine_integrity import valid_profile

bootstrap_calsuite()

from calsuite.engine import RunEngine, STATE_ERROR, STATE_RUNNING
from calsuite.export import write_samples
from calsuite.heatsource import HeatSource


DEG_C = "\N{DEGREE SIGN}C"
DEG_F = "\N{DEGREE SIGN}F"


class ConflictingUnitLink:
    is_open = True

    def __init__(self):
        self.writes = []

    def query(self, command):
        if command == "SET?":
            return "20.000000,1002"
        if command == "UNIT?":
            return "1001"
        raise AssertionError(f"unexpected query {command!r}")

    def write(self, command):
        self.writes.append(command)


class FailingRefreshLink(ConflictingUnitLink):
    def __init__(self):
        super().__init__()
        self.unit_queries = 0

    def query(self, command):
        if command == "SET?":
            return "20.000000,1001"
        if command == "UNIT?":
            self.unit_queries += 1
            if self.unit_queries >= 3:
                raise OSError("unit query unavailable")
            return "1001"
        raise AssertionError(f"unexpected query {command!r}")


class UnknownAttachedUnitLink(ConflictingUnitLink):
    def query(self, command):
        if command == "SET?":
            return "20.000000,BANANA_TOO_LONG"
        if command == "UNIT?":
            return "1001"
        raise AssertionError(f"unexpected query {command!r}")


class UnknownNumericUnitLink(UnknownAttachedUnitLink):
    def query(self, command):
        if command == "SET?":
            return "20.000000,1004"
        return super().query(command)


class UnknownLiveUnitLink(ConflictingUnitLink):
    def __init__(self):
        super().__init__()
        self.unit_queries = 0

    def query(self, command):
        if command == "SET?":
            return "20.000000,1001"
        if command == "UNIT?":
            self.unit_queries += 1
            return "BANANA" if self.unit_queries >= 3 else "1001"
        raise AssertionError(f"unexpected query {command!r}")


class UnitReplyLink:
    is_open = True

    def __init__(self, reply):
        self.reply = reply

    def query(self, _command):
        return self.reply


def source_engine(link):
    source = HeatSource({
        "name": "Conflicting well",
        "range_unit": DEG_C,
        "range_min": -100.0,
        "range_max": 200.0,
        "sp_write": "SET {value}",
        "sp_read": "SET?",
        "unit": "UNIT?",
        "enable": "",
        "disable": "",
    })
    source.link = link
    source.reported_unit = DEG_C
    profile = valid_profile()
    profile.update({
        "setpoints": [20.0],
        "enable_output": False,
        "disable_at_end": False,
    })
    engine = RunEngine(
        "run", profile, source, MinimalAdt(unit=DEG_C), FakeRegistry(),
    )
    engine.measurement_unit = DEG_C
    engine.state = STATE_RUNNING
    return engine, source


def conflicting_engine():
    return source_engine(ConflictingUnitLink())


class SourceUnitProvenanceTests(unittest.TestCase):
    def test_conflicting_exact_reply_unit_fails_and_is_preserved(self):
        engine, source = conflicting_engine()

        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("Set-point reply unit mismatch", engine.error)
        self.assertEqual(source.link.writes, ["SET 20"])
        self.assertIsInstance(engine.results, tuple)
        self.assertEqual(len(engine.results), 1)
        result = engine.results[0]
        self.assertEqual(result.verdict, "invalid")
        self.assertEqual(result.samples, ())
        self.assertEqual(result.setpoint_readback_raw, "20.000000,1002")
        self.assertEqual(result.setpoint_readback_unit_raw, "1002")
        self.assertEqual(result.setpoint_readback_unit, DEG_F)
        self.assertEqual(len(result.source_checks), 1)
        check = result.source_checks[0]
        self.assertEqual(check["readback_unit_raw"], "1002")
        self.assertEqual(check["readback_unit"], DEG_F)
        self.assertEqual(check["unit_raw"], "1001")
        self.assertEqual(check["unit"], DEG_C)
        self.assertTrue(check["unit_query_succeeded"])
        self.assertFalse(check["unit_verified"])

    def test_failed_point_exports_exact_and_verified_units_separately(self):
        engine, _source = conflicting_engine()
        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "failed-source-evidence.csv"
            write_samples(engine, engine.adt, str(path))
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.reader(handle))

        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == "Set point requested" and "Phase" in row)
        header = rows[header_index]
        row = rows[header_index + 1]
        self.assertEqual(row[header.index("Phase")], "source check")
        self.assertEqual(
            row[header.index("Heat-source set point (device text)")],
            "20.000000,1002")
        self.assertEqual(
            row[header.index(
                "Heat-source set point unit token (device text)")], "1002")
        self.assertEqual(
            row[header.index("Heat-source set point unit")], DEG_F)
        self.assertEqual(
            row[header.index("Heat-source live unit query (device text)")],
            "1001")
        self.assertEqual(
            row[header.index("Heat-source live unit query (parsed)")], DEG_C)
        self.assertEqual(
            row[header.index("Live unit query succeeded")], "yes")
        self.assertEqual(row[header.index("Unit evidence valid")], "NO")

    def test_failed_live_unit_query_never_reuses_cached_verified_evidence(self):
        engine, _source = source_engine(FailingRefreshLink())

        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        self.assertEqual(engine.state, STATE_ERROR)
        self.assertIn("unit query unavailable", engine.error)
        self.assertEqual(len(engine.results), 1)
        check = engine.results[0].source_checks[0]
        self.assertEqual(check["readback_unit_raw"], "1001")
        self.assertEqual(check["readback_unit"], DEG_C)
        self.assertFalse(check["unit_verified"])
        self.assertFalse(check["unit_query_succeeded"])
        self.assertEqual(check["unit_raw"], "")
        self.assertEqual(check["unit"], "")

    def test_long_unknown_attached_unit_is_rejected_without_replacing_token(self):
        engine, source = source_engine(UnknownAttachedUnitLink())

        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        self.assertIn("unrecognised unit token", engine.error)
        self.assertEqual(len(engine.results), 1)
        check = engine.results[0].source_checks[0]
        self.assertEqual(check["readback_unit_raw"], "BANANA_TOO_LONG")
        self.assertEqual(check["readback_unit"], "")
        self.assertTrue(check["unit_query_succeeded"])
        self.assertFalse(check["unit_verified"])
        self.assertEqual(source.unit_token, "1001")
        self.assertNotEqual(source.profile.get("unit_token"), "BANANA_TOO_LONG")

    def test_unknown_numeric_attached_unit_is_also_rejected(self):
        engine, _source = source_engine(UnknownNumericUnitLink())

        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        self.assertIn("unrecognised unit token", engine.error)
        check = engine.results[0].source_checks[0]
        self.assertEqual(check["readback_unit_raw"], "1004")
        self.assertEqual(check["readback_unit"], "")
        self.assertFalse(check["unit_verified"])

    def test_unknown_live_unit_reply_is_retained_but_not_verified(self):
        engine, _source = source_engine(UnknownLiveUnitLink())

        with patch("calsuite.heatsource.time.sleep", return_value=None):
            engine._run()

        self.assertIn("did not report its temperature unit", engine.error)
        check = engine.results[0].source_checks[0]
        self.assertEqual(check["unit_raw"], "BANANA")
        self.assertEqual(check["unit"], "")
        self.assertTrue(check["unit_query_succeeded"])
        self.assertFalse(check["unit_verified"])

    def test_dedicated_unit_query_retains_exact_device_token(self):
        cases = (("CEL", "CEL", DEG_C), ("999", "999", "\N{DEGREE SIGN}Re"))
        for reply, expected_token, expected_unit in cases:
            with self.subTest(reply=reply):
                source = HeatSource({
                    "name": "Token well", "range_unit": DEG_C,
                    "range_min": -100.0, "range_max": 200.0,
                    "unit": "UNIT?",
                })
                source.link = UnitReplyLink(reply)
                self.assertEqual(source.read_unit(), expected_unit)
                self.assertEqual(source.unit_token, expected_token)
                self.assertEqual(source.profile["unit_token"], expected_token)

    def test_conflicting_fields_in_one_unit_reply_fail_closed(self):
        for reply in ("FAH,1001", "CEL,1002"):
            with self.subTest(reply=reply):
                source = HeatSource({
                    "name": "Ambiguous well", "range_unit": DEG_C,
                    "range_min": -100.0, "range_max": 200.0,
                    "unit": "UNIT?", "unit_token": "ORIGINAL",
                })
                source.link = UnitReplyLink(reply)

                self.assertEqual(source.read_unit(), "")
                self.assertEqual(source.last_unit_reply, reply)
                self.assertEqual(source.unit_token, "ORIGINAL")
                self.assertEqual(source.profile["unit_token"], "ORIGINAL")


if __name__ == "__main__":
    unittest.main()
