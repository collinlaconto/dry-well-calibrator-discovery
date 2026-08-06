import time
import unittest

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.formats import ERROR_QUERY, NONSENSE_COMMAND
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


if __name__ == "__main__":
    unittest.main()
