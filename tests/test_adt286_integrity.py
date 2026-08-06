import dataclasses
import unittest
from unittest.mock import patch

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.adt286 import Adt286, Reading, parse_scan_data


FAULT_LOG_2_PAYLOAD = (
    '"REF1,1281,1,2026-08-06 16:58:23 861,109.592810,109.592810,'
    '1001,1,24.175371;CH1-01A,1243,1,2026-08-06 16:25:32 000,'
    '0.027320,0.027320,1001,1,24.00675,32767,0,1001,1,23.33;"'
)


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
    def test_exact_fault_log_2_hyphen_timestamps_preserve_device_values(self):
        parsed = parse_scan_data(FAULT_LOG_2_PAYLOAD)

        self.assertEqual(set(parsed), {"REF1", "CH1-01A"})
        reference = parsed["REF1"]
        dut = parsed["CH1-01A"]
        self.assertEqual(reference["raw_temperature"], "24.175371")
        self.assertEqual(reference["raw_electrical"], "109.592810")
        self.assertEqual(reference["temperature"], 24.175371)
        self.assertEqual(reference["electrical"], 109.592810)
        self.assertEqual(reference["unit"], "°C")
        self.assertEqual(reference["electrical_unit"], "Ω")
        self.assertEqual(reference["device_timestamp"],
                         "2026-08-06 16:58:23 861")
        self.assertEqual(dut["raw_temperature"], "24.00675")
        self.assertEqual(dut["raw_electrical"], "0.027320")
        self.assertEqual(dut["temperature"], 24.00675)
        self.assertEqual(dut["electrical"], 0.027320)
        self.assertEqual(dut["unit"], "°C")
        self.assertEqual(dut["electrical_unit"], "mV")
        self.assertEqual(dut["device_timestamp"],
                         "2026-08-06 16:25:32 000")

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

    def test_multiline_frame_timestamp_preserves_adjacent_first_channel(self):
        stamp = "2026:08:05 17:04:03 123"
        payload = (
            stamp + "\r\n\"" + scan_group("REF", "20.125") + ";"
            + scan_group("DUT", "20.250") + ";\""
        )

        parsed = parse_scan_data(payload)

        self.assertEqual(set(parsed), {"REF", "DUT"})
        self.assertEqual(parsed["REF"]["raw_temperature"], "20.125")
        self.assertEqual(parsed["DUT"]["raw_temperature"], "20.250")
        self.assertEqual(parsed["REF"]["device_timestamp"], stamp)
        self.assertEqual(parsed["DUT"]["device_timestamp"], stamp)

    def test_timestamp_touching_last_value_does_not_discard_device_token(self):
        stamp = "2026:08:05 17:04:03 123"

        parsed = parse_scan_data(
            scan_group("REF", "20.125000000000000001") + stamp
        )["REF"]

        self.assertEqual(parsed["raw_temperature"],
                         "20.125000000000000001")
        self.assertEqual(parsed["device_timestamp"], stamp)

    def test_ordered_frame_timestamps_are_mapped_without_host_substitution(self):
        first = "2026:08:05 17:04:03 123"
        second = "2026:08:05 17:04:03 456"
        payload = (
            first + ";" + second + ";\""
            + scan_group("REF", "20.1") + ";"
            + scan_group("DUT", "20.2") + ";\""
        )

        parsed = parse_scan_data(payload)

        self.assertEqual(parsed["REF"]["device_timestamp"], first)
        self.assertEqual(parsed["DUT"]["device_timestamp"], second)

    def test_large_timestamped_frame_preserves_every_exact_device_value(self):
        stamp = "2026:08:05 17:04:03 123"
        expected = {
            f"CH1-{index:02d}": f"{20 + index / 100:.18f}"
            for index in range(1, 43)
        }
        payload = (
            stamp + "\r\n\""
            + ";".join(scan_group(channel, value)
                       for channel, value in expected.items())
            + ";\""
        )

        parsed = parse_scan_data(payload)

        self.assertEqual(set(parsed), set(expected))
        for channel, exact_value in expected.items():
            self.assertEqual(parsed[channel]["raw_temperature"], exact_value)
            self.assertEqual(parsed[channel]["device_timestamp"], stamp)


class ReadingAndScanTests(unittest.TestCase):
    def test_exact_fault_log_2_frame_publishes_both_subscribed_channels(self):
        adt = Adt286()
        adt.link = RepeatingLink(FAULT_LOG_2_PAYLOAD)
        adt._subs = {"run": ["REF1", "CH1-01A"]}
        adt._last_poll_started = -1e9

        with patch("calsuite.adt286.time.monotonic",
                   side_effect=[10.0, 10.01]), \
             patch("calsuite.adt286.time.time",
                   return_value=1_700_000_000.0):
            self.assertEqual(adt.poll_once(), 2)

        frame = adt.snapshot(["REF1", "CH1-01A"], cycle=1)
        self.assertEqual(frame["REF1"].raw_temperature, "24.175371")
        self.assertEqual(frame["CH1-01A"].raw_temperature, "24.00675")
        self.assertEqual(frame["REF1"].device_timestamp,
                         "2026-08-06 16:58:23 861")
        self.assertEqual(frame["CH1-01A"].device_timestamp,
                         "2026-08-06 16:25:32 000")
        self.assertEqual(adt.freshness_supported, True)
        self.assertEqual(adt.recoveries, 0)

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

    def test_partial_frames_activate_recovery_instead_of_disabling_freshness(self):
        link = RepeatingLink(
            scan_group("REF", "20.0") + ",2026:08:05 17:04:03 123"
        )
        adt = Adt286()
        adt.link = link
        adt._subs = {"run": ["REF", "DUT"]}
        adt.recover_after = 2
        adt.recover_min_gap = 0.0

        with patch("calsuite.adt286.time.time", side_effect=[10.0, 20.0]):
            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 0)
            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 0)

        self.assertEqual(adt.recoveries, 1)
        self.assertIsNone(adt.freshness_supported)
        self.assertIn("missing DUT", adt.last_error)


if __name__ == "__main__":
    unittest.main()
