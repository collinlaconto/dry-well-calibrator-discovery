import dataclasses
import unittest
from unittest.mock import patch

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.adt286 import Adt286, Reading, parse_scan_data


def scan_group(channel, temperature, electrical="1.0000000000000001", unit_id=1001):
    return (
        f"{channel},1281,1,{electrical},{electrical},"
        f"{unit_id},1,{temperature}"
    )


class RepeatingLink:
    is_open = True

    def __init__(self, payload):
        self.payload = payload
        self.queries = []

    def query(self, command):
        self.queries.append(command)
        return self.payload

    def write(self, command):
        return None


class SequenceLink(RepeatingLink):
    def __init__(self, payloads):
        super().__init__(None)
        self.payloads = list(payloads)

    def query(self, command):
        self.queries.append(command)
        return self.payloads.pop(0)


def timestamped_payload(stamp, ref="20.0", dut="20.1"):
    return (
        scan_group("REF", ref) + f",{stamp};"
        + scan_group("DUT", dut) + f",{stamp}"
    )


class ParseScanDataTests(unittest.TestCase):
    def test_nonfinite_temperature_is_rejected_but_raw_token_is_retained(self):
        for token in ("nan", "inf", "-inf"):
            with self.subTest(token=token):
                parsed = parse_scan_data(scan_group("REF", token))["REF"]
                self.assertIsNone(parsed["temperature"])
                self.assertEqual(parsed["raw_temperature"], token)

    def test_nonfinite_electrical_value_is_rejected_but_raw_token_is_retained(self):
        parsed = parse_scan_data(
            scan_group("REF", "20.0", electrical="inf")
        )["REF"]
        self.assertIsNone(parsed["electrical"])
        self.assertEqual(parsed["raw_electrical"], "inf")

    def test_exact_numeric_tokens_survive_parsing(self):
        temperature = "20.12345678901234567890"
        electrical = "100.000000000000000001"
        parsed = parse_scan_data(
            scan_group("REF", temperature, electrical=electrical)
        )["REF"]
        self.assertEqual(parsed["raw_temperature"], temperature)
        self.assertEqual(parsed["raw_electrical"], electrical)


class ReadingAndScanTests(unittest.TestCase):
    def test_reading_is_immutable(self):
        reading = Reading(
            "REF", 20.0, "°C", cycle=4, timestamp=100.5,
            monotonic=50.5, raw_temperature="20.000000",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            reading.temperature = 99.0
        with self.assertRaises(dataclasses.FrozenInstanceError):
            reading.raw_temperature = "99.0"

    def test_poll_once_is_rate_limited_to_device_scan_period(self):
        link = SequenceLink([
            timestamped_payload("2026:08:05 17:04:03 123"),
            timestamped_payload("2026:08:05 17:04:04 123"),
        ])
        adt = Adt286()
        adt.link = link
        adt._subs = {"run": ["REF", "DUT"]}
        adt.scan_rate = "1000"

        with patch("calsuite.adt286.time.monotonic",
                   side_effect=[10.0, 10.01, 10.5, 11.0, 11.01]), \
             patch("calsuite.adt286.time.time", return_value=1_700_000_000.0):
            self.assertEqual(adt.poll_once(), 2)
            self.assertEqual(adt.poll_once(), 0)
            self.assertEqual(adt.poll_once(), 2)

        self.assertEqual(
            link.queries, ["SCAN:DATA:Last? 1", "SCAN:DATA:Last? 1"]
        )
        self.assertEqual(adt.cycle, 2)

    def test_snapshot_with_cycle_never_returns_mixed_cycle_data(self):
        adt = Adt286()
        adt._readings = {
            "REF": Reading("REF", 20.0, "°C", cycle=2),
            "DUT": Reading("DUT", 20.1, "°C", cycle=1),
        }

        frame = adt.snapshot(["REF", "DUT"], cycle=2)

        self.assertEqual(frame["REF"].cycle, 2)
        self.assertIsNone(frame["DUT"])

    def test_partial_poll_is_withheld_and_evicts_all_stale_channels(self):
        link = RepeatingLink(
            scan_group("REF", "20.000000000000000001")
            + ",2026:08:05 17:04:03 123"
        )
        adt = Adt286()
        adt.link = link
        adt._subs = {"run": ["REF", "DUT"]}
        adt._cycle = 1
        adt._readings = {
            "REF": Reading("REF", 19.0, "°C", cycle=1),
            "DUT": Reading("DUT", 19.1, "°C", cycle=1),
        }

        with patch("calsuite.adt286.time.monotonic",
                   side_effect=[100.0, 100.01]), \
             patch("calsuite.adt286.time.time", return_value=1_700_000_000.0):
            self.assertEqual(adt.poll_once(), 0)

        frame = adt.snapshot(["REF", "DUT"], cycle=2)
        self.assertEqual(adt.cycle, 1)
        self.assertIsNone(frame["REF"])
        self.assertIsNone(frame["DUT"])
        self.assertIsNone(adt.latest("REF"))
        self.assertIsNone(adt.latest("DUT"))


if __name__ == "__main__":
    unittest.main()
