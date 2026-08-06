import time
import unittest
from unittest.mock import patch

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.formats import ERROR_QUERY, NONSENSE_COMMAND, profile_for_model
from calsuite.heatsource import HeatSource, checked_exchange
from calsuite.transport import Link
from calsuite.ui import is_query_command


class ChunkTransport:
    is_open = True

    def __init__(self, chunks, delay=0.0):
        self.chunks = list(chunks)
        self.delay = delay
        self.writes = []
        self.spec = {}

    def _reset_input(self):
        return None

    def _write(self, data):
        self.writes.append(data)

    def _read(self, limit=4096):
        if not self.chunks:
            return b""
        if self.delay:
            time.sleep(self.delay)
        return self.chunks.pop(0)

    def close(self):
        return None


class LinkFramingTests(unittest.TestCase):
    def link(self, chunks, timeout=0.05, delay=0.0, strict=True):
        link = Link(
            reply_timeout=timeout, require_reply_terminator=strict,
            max_reply_time=1.0,
        )
        link.transport = ChunkTransport(chunks, delay=delay)
        return link

    def test_echo_and_multiline_scan_response_are_returned_complete(self):
        command = "SCAN:DATA:Last? 1"
        timestamp = "2026:08:05 17:04:03 123"
        scan = '"REF,1281,1,1.0,1.0,1001,1,20.0;"'
        link = self.link([
            (command + "\r\n" + timestamp + "\r\n").encode("ascii"),
            (scan + "\r\n").encode("ascii"),
        ])

        reply = link.query(command)

        self.assertEqual(reply, timestamp + "\n" + scan)

    def test_continuous_chunked_reply_can_outlive_initial_idle_budget(self):
        link = self.link(
            [b'"REF,1281,1,1.0,', b'1.0,1001,1,20.0;', b'"\r\n'],
            timeout=0.025, delay=0.015,
        )
        started = time.monotonic()

        reply = link.query("SCAN:DATA:Last? 1")

        self.assertGreater(time.monotonic() - started, 0.025)
        self.assertIn("REF,1281", reply)
        self.assertTrue(reply.endswith(';"'))

    def test_strict_link_rejects_unterminated_partial_frame(self):
        link = self.link([b'"REF,1281,1,1.0'], timeout=0.02)

        with self.assertRaises(TimeoutError):
            link.query("SCAN:DATA:Last? 1")


class TerminalQueryClassificationTests(unittest.TestCase):
    def test_parameterized_scpi_query_is_read_only(self):
        self.assertTrue(is_query_command("SCAN:DATA:Last? 1"))
        self.assertTrue(is_query_command('CHANnel:CONFig? "REF1"'))
        self.assertTrue(is_query_command("SYSTem:ERRor?"))

    def test_device_changing_command_is_not_query(self):
        self.assertFalse(is_query_command(
            'SCAN:MULT:STARt 1000,"REF1,CH1-01A"'))
        self.assertFalse(is_query_command("SCAN:STOP"))
        self.assertFalse(is_query_command("SCAN:DATA:Last? 1;SCAN:STOP"))
        self.assertFalse(is_query_command("SCAN:DATA:Last?\nSCAN:STOP"))


class QueueLink:
    is_open = True

    def __init__(self, stale=None):
        self.queue = list(stale or [])
        self.events = []

    def write(self, command):
        self.events.append(("write", command))
        if command == "*CLS":
            self.queue.clear()
        elif command == NONSENSE_COMMAND:
            self.queue.append('-110,"Command header error"')
        return None

    def query(self, command):
        self.events.append(("query", command))
        if command == NONSENSE_COMMAND:
            raise AssertionError("the deliberate bad header must not be queried")
        if command == ERROR_QUERY:
            return self.queue.pop(0) if self.queue else '0,"No error"'
        return "20.0000,1001"


class ReadOnlyDiscoveryLink:
    """A simulated 878 that fails immediately if discovery writes anything."""

    is_open = True

    def __init__(self):
        self.events = []
        self.setpoint = 100.0
        self.error_queue = []

    def open(self, _target=None):
        return None

    def close(self):
        return None

    def write(self, command):
        self.events.append(("write", command))
        raise AssertionError(f"read-only discovery attempted write {command!r}")

    def query(self, command):
        self.events.append(("query", command))
        replies = {
            "*IDN?": "Additel,ADT878-160,TEST,1.0",
            "TEMPerature:TARGet?": "100.000,1001",
            "MEASure:TEMPerature?": "23.500,23.500,NaN,NaN",
            "UNIT:TEMPerature?": "C,1001",
            "TEMPerature:STATus?": "0",
            "TEMP:STAB?": "0.05,1001",
        }
        if command in replies:
            return replies[command]
        self.error_queue.append(f'-110,"Unsupported query {command}"')
        return ""


class ErrorQueueTests(unittest.TestCase):
    def test_error_queue_probe_is_nonblocking_and_leaves_no_minus_110(self):
        source = HeatSource({})
        source.link = QueueLink()

        self.assertTrue(source._has_error_queue())

        self.assertNotIn(("query", NONSENSE_COMMAND), source.link.events)
        self.assertEqual(source.link.queue, [])
        self.assertEqual(source.link.events[-1], ("write", "*CLS"))

    def test_checked_success_clears_stale_minus_110_before_command(self):
        link = QueueLink(stale=['-110,"old header error"'])

        reply, error = checked_exchange(
            link, "TEMPerature?", expect_reply=True, check_error=True
        )

        self.assertEqual(reply, "20.0000,1001")
        self.assertEqual(error, '0,"No error"')
        self.assertEqual(link.events[:3], [
            ("write", "*CLS"),
            ("query", "TEMPerature?"),
            ("query", ERROR_QUERY),
        ])
        self.assertEqual(link.events[-1], ("write", "*CLS"))

    def test_explicit_error_query_is_not_precleared(self):
        stale = '-110,"old header error"'
        link = QueueLink(stale=[stale])

        reply, automatic_error = checked_exchange(
            link, ERROR_QUERY, expect_reply=True, check_error=True
        )

        self.assertEqual(reply, stale)
        self.assertIsNone(automatic_error)
        self.assertEqual(link.events, [("query", ERROR_QUERY)])


class ReadOnlyDiscoveryTests(unittest.TestCase):
    def source(self, profile=None):
        source = HeatSource(profile or profile_for_model("878-160"))
        source.link = ReadOnlyDiscoveryLink()
        return source

    def assert_only_queries(self, source):
        self.assertTrue(source.link.events)
        self.assertTrue(all(kind == "query"
                            for kind, _command in source.link.events))
        commands = [command for _kind, command in source.link.events]
        self.assertNotIn("*CLS", commands)
        self.assertNotIn(ERROR_QUERY, commands)
        self.assertNotIn(NONSENSE_COMMAND, commands)

    def test_verify_commands_is_strictly_read_only_and_infers_additel_write(self):
        source = self.source()
        before = source.link.setpoint

        report = source.verify_commands(test_delta=50, restore=False)

        self.assert_only_queries(source)
        self.assertEqual(source.link.setpoint, before)
        self.assertTrue(report["read_only"])
        self.assertTrue(report["write_inferred"])
        self.assertFalse(report["verified"])
        self.assertEqual(report["write_command"],
                         "TEMPerature:TARGet {value},{unit}")
        self.assertEqual(source.profile["sp_write"],
                         "TEMPerature:TARGet {value},{unit}")
        self.assertFalse(source.profile["verified"])
        self.assertEqual(source.profile["enable"], "")
        self.assertEqual(source.profile["disable"], "")
        self.assertNotIn("enable", report["adopted"])
        self.assertNotIn("disable", report["adopted"])
        self.assertEqual(report["control_status_query"],
                         "TEMPerature:STATus?")
        self.assertEqual(report["control_status_reply"], "0")
        self.assertEqual(source.link.error_queue, [])
        self.assertEqual(
            [command for _kind, command in source.link.events],
            ["*IDN?", "TEMPerature:TARGet?", "MEASure:TEMPerature?",
             "UNIT:TEMPerature?", "TEMPerature:STATus?"],
        )

    def test_malicious_stored_read_command_is_refused_without_transmission(self):
        unsafe_commands = (
            "TEMPerature:TARGet 150",
            "TEMPerature:TARGet?;TEMPerature:TARGet 150",
            ERROR_QUERY,
        )
        for unsafe in unsafe_commands:
            with self.subTest(command=unsafe):
                source = self.source()
                source.profile["sp_read"] = unsafe

                report = source.verify_commands()

                transmitted = [command for _kind, command
                               in source.link.events]
                self.assertNotIn(unsafe, transmitted)
                self.assertEqual(source.link.setpoint, 100.0)
                self.assertEqual(report["adopted"]["sp_read"],
                                 "TEMPerature:TARGet?")
                self.assert_only_queries(source)
                self.assertEqual(source.link.error_queue, [])

    def test_known_family_ignores_safe_looking_non_authoritative_override(self):
        source = self.source()
        source.profile["sp_read"] = "BOGUS?"

        report = source.verify_commands()

        commands = [command for _kind, command in source.link.events]
        self.assertNotIn("BOGUS?", commands)
        self.assertEqual(report["adopted"]["sp_read"],
                         "TEMPerature:TARGet?")
        self.assertEqual(source.link.error_queue, [])

    def test_runtime_read_paths_reject_stored_writes_before_transmission(self):
        cases = (
            ("sp_read", "read_setpoint"),
            ("unit", "read_unit"),
            ("value", "read_temperature"),
        )
        unsafe = "TEMPerature:TARGet 150"
        for key, method_name in cases:
            with self.subTest(key=key):
                profile = profile_for_model("878-160")
                source = self.source(profile)
                source.profile[key] = unsafe

                with self.assertRaisesRegex(RuntimeError, "safe read query"):
                    getattr(source, method_name)()

                self.assertNotIn(unsafe, [
                    command for _kind, command in source.link.events
                ])
                self.assertEqual(source.link.setpoint, 100.0)
                self.assertEqual(source.link.error_queue, [])

    def test_connect_refuses_unsafe_stored_reads_without_sending_them(self):
        profile = profile_for_model("878-160")
        unsafe_sp = "TEMPerature:TARGet 150"
        unsafe_unit = "UNIT:TEMPerature?;TEMPerature:TARGet 150"
        link = ReadOnlyDiscoveryLink()
        source = HeatSource(profile)
        source.profile["sp_read"] = unsafe_sp
        source.profile["unit"] = unsafe_unit

        with patch("calsuite.heatsource.make_link", return_value=link):
            source.connect({"kind": "tcp", "host": "192.0.2.1",
                            "tcp_port": 8000})

        commands = [command for _kind, command in link.events]
        self.assertEqual(commands, ["*IDN?"])
        self.assertNotIn(unsafe_sp, commands)
        self.assertNotIn(unsafe_unit, commands)
        self.assertEqual(link.setpoint, 100.0)
        self.assertEqual(link.error_queue, [])

    def test_legacy_additel_outp_defaults_are_removed_on_load(self):
        profile = profile_for_model("878-160")
        profile["enable"] = " OUTP:STAT   1 "
        profile["disable"] = "outp:stat 0"

        source = HeatSource(profile)

        self.assertEqual(source.profile["enable"], "")
        self.assertEqual(source.profile["disable"], "")
        self.assertFalse(source.profile["verified"])

    def test_known_family_read_overrides_are_migrated_to_authoritative_forms(self):
        profile = profile_for_model("878-160")
        profile["sp_read"] = "BOGUS?"
        profile["value"] = "OTHER?"
        profile["unit"] = "UNITS?"

        source = HeatSource(profile)

        self.assertEqual(source.profile["sp_read"],
                         "TEMPerature:TARGet?")
        self.assertEqual(source.profile["value"],
                         "MEASure:TEMPerature?")
        self.assertEqual(source.profile["unit"],
                         "UNIT:TEMPerature?")

    def test_setpoint_sweep_is_query_only_and_leaves_write_unverified(self):
        source = self.source()
        before = source.link.setpoint

        result = source.sweep("sp_read")

        self.assert_only_queries(source)
        self.assertEqual(source.link.setpoint, before)
        self.assertEqual(result["winner"], "TEMPerature:TARGet?")
        self.assertTrue(result["read_only"])
        self.assertTrue(result["write_inferred"])
        self.assertFalse(result["verified"])
        self.assertEqual(source.profile["sp_write"],
                         "TEMPerature:TARGet {value},{unit}")
        self.assertFalse(source.profile["verified"])
        self.assertEqual(source.link.error_queue, [])


if __name__ == "__main__":
    unittest.main()
