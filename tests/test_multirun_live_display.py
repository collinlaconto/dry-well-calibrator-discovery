"""Regressions for concurrent-run scan fan-out and live UI refreshes."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import queue
import re
import threading
import unittest
from unittest.mock import Mock

from tests.bootstrap import bootstrap_calsuite

bootstrap_calsuite()

from calsuite.adt286 import Adt286, Reading
from calsuite import theme
from calsuite.engine import RunEngine, default_profile
from calsuite.ui import SuiteApp


DEG_C = "\N{DEGREE SIGN}C"


def reading(channel, value, cycle=7, timestamp=1_700_000_007.0):
    return Reading(
        channel,
        value,
        DEG_C,
        cycle=cycle,
        timestamp=timestamp,
        monotonic=700.0 + cycle,
        device_timestamp=f"2026-08-06 17:00:{cycle:02d} 000",
        raw_temperature=f"{value:.9f}",
    )


def scan_group(channel, value, stamp=""):
    group = f"{channel},1281,1,1.0,1.0,1001,1,{value}"
    return group + (f",{stamp}" if stamp else "")


class PayloadLink:
    is_open = True

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.writes = []

    def query(self, _command):
        return self.payloads.pop(0)

    def write(self, command):
        self.writes.append(command)


def active_engine(run_id, reference, dut):
    return SimpleNamespace(
        run_id=run_id,
        is_active=True,
        profile={
            "name": run_id,
            "reference_channel": reference,
            "dut_channels": [dut],
            "setpoints": [20.0],
            "tolerance": 0.05,
            "stability_window": 30.0,
        },
        heat_source=SimpleNamespace(name=f"well-{run_id}"),
        phase="waiting for stability",
        state="running",
        evidence={"readout_unit": DEG_C},
        results=[],
        current_setpoint=20.0,
        current_index=0,
    )


class LiveCacheConcurrencyTests(unittest.TestCase):
    def test_cached_reads_never_wait_for_blocking_serial_io(self):
        transport_held = threading.Event()
        release_transport = threading.Event()

        class BlockingLink:
            is_open = True

            def query(self, _command):
                transport_held.set()
                release_transport.wait(2.0)
                return ";".join((
                    scan_group("REF1", "21.0"),
                    scan_group("DUT1", "21.1"),
                    scan_group("REF2", "31.0"),
                    scan_group("DUT2", "31.1"),
                ))

            def write(self, _command):
                return None

        adt = Adt286()
        adt.link = BlockingLink()
        adt._subs = {
            "run1": ["REF1", "DUT1"],
            "run2": ["REF2", "DUT2"],
        }
        adt._readings = {
            channel: reading(channel, value)
            for channel, value in {
                "REF1": 20.0, "DUT1": 20.1,
                "REF2": 30.0, "DUT2": 30.1,
            }.items()
        }
        adt._cycle = 7
        adt._last_poll_started = -1e9

        read_finished = threading.Event()
        observed = {}

        def read_cache():
            observed["latest"] = adt.latest("REF1")
            observed["frame"] = adt.snapshot(
                ["REF1", "DUT1", "REF2", "DUT2"])
            observed["cycle"] = adt.cycle
            observed["health"] = adt.health
            read_finished.set()

        holder = threading.Thread(target=adt.poll_once, daemon=True)
        reader = threading.Thread(target=read_cache, daemon=True)
        holder.start()
        self.assertTrue(transport_held.wait(1.0))
        reader.start()
        try:
            self.assertTrue(
                read_finished.wait(0.5),
                "live cache reads blocked behind the serial transaction",
            )
        finally:
            release_transport.set()
            holder.join(1.0)
            reader.join(1.0)

        self.assertEqual(observed["latest"].temperature, 20.0)
        self.assertEqual(observed["cycle"], 7)
        self.assertEqual(
            {item.cycle for item in observed["frame"].values()}, {7})
        self.assertEqual(observed["health"], "scanning")

    def test_poll_validates_the_same_union_that_configured_the_device(self):
        class ConfiguredLink:
            is_open = True

            def __init__(self):
                self.channels = ["REF1", "DUT1"]
                self.operations = []

            def write(self, command):
                self.operations.append(("write", command))
                match = re.search(r'"([^"]*)"', command)
                if match:
                    self.channels = match.group(1).split(",")

            def query(self, command):
                self.operations.append(("query", command))
                groups = []
                for index, channel in enumerate(self.channels):
                    value = 20.0 + index / 10.0
                    groups.append(
                        f"{channel},1281,1,1.0,1.0,1001,1,{value}"
                    )
                return ";".join(groups)

        adt = Adt286()
        adt.link = ConfiguredLink()
        adt._subs = {"run1": ["REF1", "DUT1"]}
        adt._last_poll_started = -1e9
        captured = threading.Event()
        continue_poll = threading.Event()
        original_subscribed = adt.subscribed_channels

        def delayed_subscribed_channels():
            channels = original_subscribed()
            captured.set()
            continue_poll.wait(2.0)
            return channels

        adt.subscribed_channels = delayed_subscribed_channels
        poller = threading.Thread(target=adt.poll_once, daemon=True)
        subscriber = threading.Thread(
            target=lambda: adt.subscribe("run2", ["REF2", "DUT2"]),
            daemon=True,
        )

        poller.start()
        self.assertTrue(captured.wait(1.0))
        subscriber.start()
        continue_poll.set()
        poller.join(2.0)
        subscriber.join(2.0)

        self.assertFalse(poller.is_alive())
        self.assertFalse(subscriber.is_alive())
        self.assertEqual(
            [operation for operation, _command in adt.link.operations],
            ["query", "write"],
        )
        self.assertEqual(
            adt.subscribed_channels(),
            ["REF1", "DUT1", "REF2", "DUT2"],
        )

    def test_bad_second_run_never_blanks_a_complete_first_run_frame(self):
        adt = Adt286()
        adt.link = PayloadLink([
            ";".join((scan_group("REF1", "20.0"),
                      scan_group("DUT1", "20.1")))
        ])
        adt._subs = {
            "run1": ["REF1", "DUT1"],
            "run2": ["REF2", "DUT2"],
        }
        adt._last_poll_started = -1e9

        updated = adt.poll_once()

        self.assertEqual(updated, 2)
        run1 = adt.snapshot(["REF1", "DUT1"])
        run2 = adt.snapshot(["REF2", "DUT2"])
        self.assertEqual(
            {channel: item.temperature for channel, item in run1.items()},
            {"REF1": 20.0, "DUT1": 20.1},
        )
        self.assertEqual(run1["REF1"].cycle, run1["DUT1"].cycle)
        self.assertEqual(run2, {"REF2": None, "DUT2": None})
        self.assertEqual(adt.recoveries, 0)

    def test_stale_second_run_does_not_hold_back_fresh_first_run(self):
        stamp1 = "2026-08-06 17:00:01 000"
        stamp2 = "2026-08-06 17:00:02 000"
        first = ";".join((
            scan_group("REF1", "20.0", stamp1),
            scan_group("DUT1", "20.1", stamp1),
            scan_group("REF2", "30.0", stamp1),
            scan_group("DUT2", "30.1", stamp1),
        ))
        staggered = ";".join((
            scan_group("REF1", "20.2", stamp2),
            scan_group("DUT1", "20.3", stamp2),
            scan_group("REF2", "99.0", stamp1),
            scan_group("DUT2", "99.1", stamp1),
        ))
        adt = Adt286()
        adt.link = PayloadLink([first, staggered])
        adt._subs = {
            "run1": ["REF1", "DUT1"],
            "run2": ["REF2", "DUT2"],
        }

        adt._last_poll_started = -1e9
        self.assertEqual(adt.poll_once(), 4)
        adt._last_poll_started = -1e9
        self.assertEqual(adt.poll_once(), 2)

        run1 = adt.snapshot(["REF1", "DUT1"])
        run2 = adt.snapshot(["REF2", "DUT2"])
        self.assertEqual(run1["REF1"].temperature, 20.2)
        self.assertEqual(run1["DUT1"].temperature, 20.3)
        self.assertEqual(run1["REF1"].cycle, 2)
        self.assertEqual(run2["REF2"].temperature, 30.0)
        self.assertEqual(run2["DUT2"].temperature, 30.1)
        self.assertEqual(run2["REF2"].cycle, 1)

    def test_subscribing_run_two_keeps_run_ones_last_device_frame_visible(self):
        adt = Adt286()
        adt.link = PayloadLink([])
        adt._subs = {"run1": ["REF1", "DUT1"]}
        adt._group_failures = {"run1": 0}
        adt._scan_channels = ("REF1", "DUT1")
        adt._readings = {
            "REF1": reading("REF1", 20.0),
            "DUT1": reading("DUT1", 20.1),
        }

        adt.subscribe("run2", ["REF2", "DUT2"])

        run1 = adt.snapshot(["REF1", "DUT1"])
        run2 = adt.snapshot(["REF2", "DUT2"])
        self.assertEqual(run1["REF1"].temperature, 20.0)
        self.assertEqual(run1["DUT1"].temperature, 20.1)
        self.assertEqual(run2, {"REF2": None, "DUT2": None})

    def test_persistently_bad_run_two_recovers_without_erasing_run_one(self):
        payloads = [
            ";".join((scan_group("REF1", str(20.0 + index / 10.0)),
                      scan_group("DUT1", str(20.1 + index / 10.0))))
            for index in range(5)
        ]
        adt = Adt286()
        adt.link = PayloadLink(payloads)
        adt._subs = {
            "run1": ["REF1", "DUT1"],
            "run2": ["REF2", "DUT2"],
        }
        adt._group_failures = {"run1": 0, "run2": 0}
        adt._scan_channels = ("REF1", "DUT1", "REF2", "DUT2")

        for index in range(5):
            adt._last_poll_started = -1e9
            self.assertEqual(adt.poll_once(), 2)
            if index == 0:
                self.assertIn("1 run frame(s) waiting", adt.health)

        self.assertEqual(adt.recoveries, 1)
        self.assertTrue(any(command.startswith("SCAN:MULT:STARt")
                            for command in adt.link.writes))
        self.assertAlmostEqual(adt.latest("REF1").temperature, 20.4)
        self.assertAlmostEqual(adt.latest("DUT1").temperature, 20.5)
        self.assertIsNone(adt.latest("REF2"))
        self.assertIsNone(adt.latest("DUT2"))

    def test_large_slow_union_recovers_before_engine_data_timeout(self):
        adt = Adt286()
        adt.poll_interval = 5.0
        adt.scan_rate = "4000"

        union = [f"CH{index}" for index in range(8)]
        threshold = adt._group_recover_after(union[:2], union)

        self.assertEqual(threshold, 7)
        self.assertGreater(threshold * adt.poll_interval, 15.0)
        self.assertLess((threshold + 1) * adt.poll_interval, 45.0)

        adt.poll_interval = 10.0
        slow_threshold = adt._group_recover_after(union[:2], union)
        self.assertLess((slow_threshold + 1) * adt.poll_interval, 45.0)

    def test_late_group_can_complete_slow_union_without_premature_restart(self):
        def payload(index, run2_stamp):
            run1_stamp = f"2026-08-06 17:00:{index:02d} 000"
            return ";".join((
                scan_group("REF1", str(20 + index / 10), run1_stamp),
                scan_group("DUT1", str(20.1 + index / 10), run1_stamp),
                scan_group("REF2", str(30 + index / 10), run2_stamp),
                scan_group("DUT2", str(30.1 + index / 10), run2_stamp),
            ))

        first_stamp = "2026-08-06 17:00:01 000"
        payloads = [payload(1, first_stamp)]
        payloads.extend(payload(index, first_stamp) for index in range(2, 6))
        payloads.append(payload(6, "2026-08-06 17:00:06 000"))
        adt = Adt286()
        adt.link = PayloadLink(payloads)
        adt.scan_rate = "4000"
        adt.poll_interval = 5.0
        adt._subs = {
            "run1": ["REF1", "DUT1"],
            "run2": ["REF2", "DUT2"],
        }
        adt._group_failures = {"run1": 0, "run2": 0}
        adt._scan_channels = ("REF1", "DUT1", "REF2", "DUT2")

        updates = []
        for _ in payloads:
            adt._last_poll_started = -1e9
            updates.append(adt.poll_once())

        self.assertEqual(updates, [4, 2, 2, 2, 2, 4])
        self.assertEqual(adt.recoveries, 0)
        self.assertEqual(adt.link.writes, [])
        self.assertAlmostEqual(adt.latest("REF2").temperature, 30.6)


class AtomicUiFrameTests(unittest.TestCase):
    def test_display_frame_uses_one_atomic_snapshot_not_latest_then_snapshot(self):
        engine = active_engine("run1", "REF1", "DUT1")
        adt = SimpleNamespace(
            latest=Mock(side_effect=AssertionError("split cache read")),
            snapshot=Mock(return_value={
                "REF1": reading("REF1", 20.0),
                "DUT1": reading("DUT1", 20.1),
            }),
        )
        app = SuiteApp.__new__(SuiteApp)
        app.adt = adt
        app._ui_scan_frame = None

        values, acquired, cycle = app._display_frame(engine)

        self.assertEqual(values, {"REF1": 20.0, "DUT1": 20.1})
        self.assertEqual(acquired, 1_700_000_007.0)
        self.assertEqual(cycle, 7)
        adt.latest.assert_not_called()
        adt.snapshot.assert_called_once_with(["REF1", "DUT1"])

    def test_one_ui_capture_fans_out_distinct_frames_to_two_runs(self):
        run1 = active_engine("run1", "REF1", "DUT1")
        run2 = active_engine("run2", "REF2", "DUT2")
        physical_frame = {
            "REF1": reading("REF1", 20.0),
            "DUT1": reading("DUT1", 20.1),
            "REF2": reading("REF2", 30.0),
            "DUT2": reading("DUT2", 30.2),
        }
        adt = SimpleNamespace(snapshot=Mock(return_value=physical_frame))
        app = SuiteApp.__new__(SuiteApp)
        app.adt = adt
        app.engines = {"run1": run1, "run2": run2}

        app._ui_scan_frame = app._capture_ui_scan()
        values1, _time1, cycle1 = app._display_frame(run1)
        values2, _time2, cycle2 = app._display_frame(run2)

        adt.snapshot.assert_called_once_with(
            ["REF1", "DUT1", "REF2", "DUT2"])
        self.assertEqual(values1, {"REF1": 20.0, "DUT1": 20.1})
        self.assertEqual(values2, {"REF2": 30.0, "DUT2": 30.2})
        self.assertEqual((cycle1, cycle2), (7, 7))

    def test_mixed_cycle_value_is_never_presented_as_current(self):
        engine = active_engine("run1", "REF1", "DUT1")
        app = SuiteApp.__new__(SuiteApp)
        app.adt = SimpleNamespace(snapshot=Mock(return_value={
            "REF1": reading("REF1", 20.0, cycle=8),
            "DUT1": reading("DUT1", 99.0, cycle=7),
        }))
        app._ui_scan_frame = None

        values, _acquired, cycle = app._display_frame(engine)

        self.assertEqual(values, {"REF1": 20.0})
        self.assertEqual(cycle, 8)

    def test_two_real_engines_can_read_the_same_physical_cycle_independently(self):
        adt = Adt286()
        adt.unit = DEG_C
        adt._cycle = 7
        adt._readings = {
            "REF1": reading("REF1", 20.0),
            "DUT1": reading("DUT1", 20.1),
            "REF2": reading("REF2", 30.0),
            "DUT2": reading("DUT2", 30.2),
        }

        engines = []
        for run_id, reference, dut in (
                ("run1", "REF1", "DUT1"),
                ("run2", "REF2", "DUT2")):
            profile = default_profile(run_id)
            profile.update({
                "reference_channel": reference,
                "dut_channels": [dut],
            })
            engine = RunEngine(run_id, profile, None, adt, None)
            engine.measurement_unit = DEG_C
            engines.append(engine)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(
                lambda engine: engine._fresh_sample_frame(0), engines))

        self.assertEqual([cycle for cycle, _frame in results], [7, 7])
        self.assertEqual(set(results[0][1]), {"REF1", "DUT1"})
        self.assertEqual(set(results[1][1]), {"REF2", "DUT2"})


class SpyStrip:
    def __init__(self, manager=""):
        self.manager = manager
        self.selected = None
        self.references = []
        self.errors = []

    def set_selected(self, selected):
        self.selected = selected

    def winfo_manager(self):
        return self.manager

    def pack_forget(self):
        self.manager = ""

    def pack(self, **_kwargs):
        self.manager = "pack"

    def update_head(self, *_args):
        return None

    def update_values(self, reference, *_args):
        self.references.append(reference)

    def update_channel(self, channel, error, tolerance):
        self.errors.append((channel, error, tolerance))


class SpyLabel:
    def configure(self, **_kwargs):
        return None

    def pack(self, **_kwargs):
        return None

    def pack_forget(self):
        return None

    def insert(self, *_args):
        return None

    def see(self, *_args):
        return None


class EndlessLogQueue:
    def __init__(self):
        self.reads = 0

    def get_nowait(self):
        self.reads += 1
        return "INFO", "busy producer"

    def put(self, _item):
        return None


class RunRackLayoutTests(unittest.TestCase):
    def test_focused_run_is_not_duplicated_in_compact_rack(self):
        app = SuiteApp.__new__(SuiteApp)
        app.engines = {
            "run1": active_engine("run1", "REF1", "DUT1"),
            "run2": active_engine("run2", "REF2", "DUT2"),
        }
        app.selected_run = "run1"
        app.strips = {
            "run1": SpyStrip(manager="pack"),
            "run2": SpyStrip(manager=""),
        }

        app._layout_run_strips()

        self.assertEqual(app.strips["run1"].manager, "")
        self.assertEqual(app.strips["run2"].manager, "pack")

    def test_focus_swaps_preserve_engine_order_in_the_compact_rack(self):
        pack_order = []

        class OrderedStrip(SpyStrip):
            def __init__(self, run_id):
                super().__init__()
                self.run_id = run_id

            def pack(self, **kwargs):
                super().pack(**kwargs)
                pack_order.append(self.run_id)

        app = SuiteApp.__new__(SuiteApp)
        app.engines = {
            run_id: active_engine(run_id, f"REF{index}", f"DUT{index}")
            for index, run_id in enumerate(("run1", "run2", "run3"), 1)
        }
        app.strips = {run_id: OrderedStrip(run_id)
                      for run_id in app.engines}
        app.selected_run = "run1"

        app._layout_run_strips()
        app.selected_run = "run2"
        app._layout_run_strips()

        self.assertEqual(pack_order, ["run2", "run3", "run1", "run3"])

    def test_finished_focus_hands_monitor_to_an_active_run(self):
        run1 = active_engine("run1", "REF1", "DUT1")
        run2 = active_engine("run2", "REF2", "DUT2")
        run1.is_active = False
        app = SuiteApp.__new__(SuiteApp)
        app.engines = {"run1": run1, "run2": run2}
        app.selected_run = "run1"

        self.assertEqual(app._selected_run_id(), "run2")
        self.assertEqual(app.selected_run, "run2")

    def test_every_run_strip_receives_each_new_shared_device_frame(self):
        run1 = active_engine("run1", "REF1", "DUT1")
        run2 = active_engine("run2", "REF2", "DUT2")
        app = SuiteApp.__new__(SuiteApp)
        app.adt = SimpleNamespace()
        app.engines = {"run1": run1, "run2": run2}
        app.selected_run = "run1"
        app.strips = {
            "run1": SpyStrip(manager=""),
            "run2": SpyStrip(manager="pack"),
        }
        app._reference_progress = {}
        app._refresh_monitor = Mock()
        app.lbl_runcount = SpyLabel()
        app.lbl_empty = SpyLabel()

        app._ui_scan_frame = {
            "REF1": reading("REF1", 20.0, cycle=7),
            "DUT1": reading("DUT1", 20.1, cycle=7),
            "REF2": reading("REF2", 30.0, cycle=7),
            "DUT2": reading("DUT2", 30.2, cycle=7),
        }
        app._refresh_run_table()
        app._ui_scan_frame = {
            "REF1": reading("REF1", 20.5, cycle=8),
            "DUT1": reading("DUT1", 20.7, cycle=8),
            "REF2": reading("REF2", 30.5, cycle=8),
            "DUT2": reading("DUT2", 30.8, cycle=8),
        }
        app._refresh_run_table()

        self.assertEqual(app.strips["run1"].references, ["20.0000", "20.5000"])
        self.assertEqual(app.strips["run2"].references, ["30.0000", "30.5000"])
        self.assertEqual(
            [round(item[1], 3) for item in app.strips["run1"].errors],
            [0.1, 0.2],
        )
        self.assertEqual(
            [round(item[1], 3) for item in app.strips["run2"].errors],
            [0.2, 0.3],
        )

    def test_reference_progress_is_kept_separate_for_each_run(self):
        run1 = active_engine("run1", "REF1", "DUT1")
        run2 = active_engine("run2", "REF2", "DUT2")
        app = SuiteApp.__new__(SuiteApp)
        app._reference_progress = {}

        app._handle_event({
            "kind": "reference", "run_id": "run1",
            "elapsed": 12.4, "span": 0.01,
        })
        app._handle_event({
            "kind": "reference", "run_id": "run2",
            "elapsed": 4.2, "span": 0.02,
        })

        self.assertEqual(app._flat_text(run1), "12 of 30 s")
        self.assertEqual(app._flat_text(run2), "4 of 30 s")

    def test_partial_run_frame_is_labeled_incomplete_even_with_reference(self):
        class EmptyTable:
            def get_children(self):
                return ()

            def delete(self, _row):
                return None

            def insert(self, *_args, **_kwargs):
                return None

        class CaptureLabel:
            text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", self.text)

        engine = active_engine("run1", "REF1", "DUT1")
        app = SuiteApp.__new__(SuiteApp)
        app.engines = {"run1": engine}
        app.selected_run = "run1"
        app.tbl_live = EmptyTable()
        app.lbl_live_note = CaptureLabel()
        app._display_frame = Mock(return_value=(
            {"REF1": 20.0}, 1_700_000_000.0, 7))

        app._refresh_live_panel()

        self.assertIn("missing DUT1", app.lbl_live_note.text)
        self.assertIn("No value is being substituted", app.lbl_live_note.text)

    def test_busy_queues_cannot_starve_refresh_or_rescheduling(self):
        app = SuiteApp.__new__(SuiteApp)
        app.log_queue = EndlessLogQueue()
        app.events = queue.Queue()
        app.log_main = SpyLabel()
        app.engines = {}
        app.adt = SimpleNamespace(health="scanning")
        app.lbl_scan = SpyLabel()
        app.nb = SimpleNamespace(set_status=Mock())
        app._capture_ui_scan = Mock(return_value={})
        app._refresh_run_table = Mock()
        app._refresh_live_panel = Mock()
        app.after = Mock()
        app._ui_scan_frame = None
        app._last_refresh_error = ""

        app._drain()

        self.assertEqual(app.log_queue.reads, 100)
        app._refresh_run_table.assert_called_once_with()
        app._refresh_live_panel.assert_called_once_with()
        app.after.assert_called_once_with(300, app._drain)

    def test_refresh_error_is_logged_and_next_refresh_is_still_scheduled(self):
        app = SuiteApp.__new__(SuiteApp)
        app.log_queue = queue.Queue()
        app.events = queue.Queue()
        app.log_main = SpyLabel()
        app.engines = {}
        app.adt = SimpleNamespace(health="scanning")
        app.lbl_scan = SpyLabel()
        app.nb = SimpleNamespace(set_status=Mock())
        app._capture_ui_scan = Mock(return_value={})
        app._refresh_run_table = Mock(side_effect=RuntimeError("paint failed"))
        app._refresh_live_panel = Mock()
        app.after = Mock()
        app._ui_scan_frame = None
        app._last_refresh_error = ""

        app._drain()

        self.assertEqual(
            app.log_queue.get_nowait(),
            ("FAIL", "Runs display refresh failed: paint failed"),
        )
        self.assertIsNone(app._ui_scan_frame)
        app.after.assert_called_once_with(300, app._drain)

    def test_mouse_wheel_over_nested_run_widget_scrolls_runs_page(self):
        page = SimpleNamespace(master=None)
        child = SimpleNamespace(master=page)
        canvas = SimpleNamespace(yview_scroll=Mock())
        app = SuiteApp.__new__(SuiteApp)
        app.runs_page = page
        app.runs_canvas = canvas

        result = app._scroll_runs_wheel(
            SimpleNamespace(widget=child, delta=-120, num=None))

        self.assertEqual(result, "break")
        canvas.yview_scroll.assert_called_once_with(1, "units")

    def test_selection_binding_reaches_nested_band_widgets(self):
        class WidgetNode:
            def __init__(self, children=()):
                self.children = list(children)
                self.callback = None

            def bind(self, event, callback):
                self.callback = (event, callback)

            def winfo_children(self):
                return self.children

        band = WidgetNode()
        row = WidgetNode([band])
        card = WidgetNode([row])
        card._clicked = Mock()

        theme.RunStrip._bind_selection_tree(card)

        self.assertEqual(band.callback[0], "<Button-1>")
        band.callback[1]()
        card._clicked.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
