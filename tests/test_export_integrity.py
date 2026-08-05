import csv
from pathlib import Path
import re
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest

from tests.bootstrap import bootstrap_calsuite
from tests.helpers import FakeHeatSource, MinimalAdt

bootstrap_calsuite()

from calsuite.engine import SetPointResult
from calsuite.export import _metadata_rows, export_run, write_samples


def export_fixture(result_setup=None):
    profile = {
        "name": "Evidence run",
        "reference_channel": "REF",
        "dut_channels": ["DUT"],
        "setpoints": [20.0],
        "tolerance_mode": "single",
        "tolerance": 0.05,
        "stability_band": 0.02,
        "stability_window": 60.0,
        "max_wait": 600.0,
        "sample_count": 1,
        "sample_interval": 0.0,
        "soak_seconds": 0.0,
    }
    result = SetPointResult(20.0, "°C", 0.05, expected_samples=1)
    result.setpoint_readback = 20.0
    result.setpoint_confirmed = True
    result.stable = True
    result.samples = [MappingProxyType({
        "t": 1_700_000_000.123456,
        "device_timestamp": "2023:11:14 22:13:20 123",
        "cycle": 42,
        "source": "ADT286 SCAN:DATA:Last? 1",
        "ref": 20.123456789,
        "ref_raw": "20.123456789012345678",
        "duts": MappingProxyType({"DUT": 20.223456789}),
        "duts_raw": MappingProxyType({"DUT": "20.223456789012345678"}),
        "units": MappingProxyType({"REF": "°C", "DUT": "°C"}),
    })]
    if result_setup is not None:
        result_setup(result)
    result.summarise(["DUT"])

    heat = FakeHeatSource(unit="°F", reported_unit="°F")
    heat.name = "Mutated Well"
    heat.idn = "MUTATED-ID"
    heat.connection = "MUTATED-CONNECTION"
    heat.range = (-459.67, 1000.0)
    adt = MinimalAdt(unit="°F")
    adt.idn = "MUTATED-ADT-ID"
    evidence = MappingProxyType({
        "readout": "Additel ADT286",
        "readout_identity": "PINNED-ADT-ID",
        "readout_connection": "PINNED-ADT-CONNECTION",
        "readout_unit": "°C",
        "channel_configuration": MappingProxyType({}),
        "heat_source": "Pinned Well",
        "heat_source_identity": "PINNED-WELL-ID",
        "heat_source_connection": "PINNED-WELL-CONNECTION",
        "heat_source_unit": "°C",
        "heat_source_range": (0.0, 100.0),
        "acquisition_command": "SCAN:DATA:Last? 1",
    })
    engine = SimpleNamespace(
        profile=profile,
        heat_source=heat,
        evidence=evidence,
        results=[result],
        state="complete",
        error="",
        started_at=1_700_000_000.0,
        finished_at=1_700_000_001.0,
    )
    return engine, adt


class ExportIntegrityTests(unittest.TestCase):
    def test_metadata_uses_pinned_run_evidence(self):
        engine, adt = export_fixture()
        rows = _metadata_rows(engine, adt)
        metadata = {row[0]: row[1] for row in rows if len(row) >= 2}

        self.assertEqual(metadata["Heat source"], "Pinned Well")
        self.assertEqual(metadata["Heat source identity"], "PINNED-WELL-ID")
        self.assertEqual(
            metadata["Heat source connection"], "PINNED-WELL-CONNECTION"
        )
        self.assertEqual(metadata["Heat source range"], "0 to 100 °C")
        self.assertEqual(metadata["Readout identity"], "PINNED-ADT-ID")
        self.assertEqual(metadata["Readout connection"], "PINNED-ADT-CONNECTION")
        self.assertEqual(metadata["Readout unit"], "°C")

    def test_raw_export_preserves_tokens_and_subsecond_time_without_derivations(self):
        engine, adt = export_fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "samples.csv"
            write_samples(engine, adt, str(path))
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.reader(handle))

        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == "Set point requested"
            and "Host receipt time" in row
        )
        header = rows[header_index]
        data = rows[header_index + 1]
        lowered = " ".join(header).lower()

        for derived in ("error", "mean", "standard deviation", " sd", "result",
                        "tolerance"):
            self.assertNotIn(derived, lowered)
        self.assertEqual(data[header.index("Reference (°C)")],
                         "20.123456789012345678")
        self.assertEqual(data[header.index("DUT (°C)")],
                         "20.223456789012345678")
        self.assertEqual(data[header.index("Scan cycle")], "42")
        self.assertEqual(data[header.index("Device acquisition time")],
                         "2023:11:14 22:13:20 123")
        self.assertEqual(data[header.index("Source")],
                         "ADT286 SCAN:DATA:Last? 1")
        timestamp = data[header.index("Host receipt time")]
        self.assertRegex(timestamp, r"\.\d{6}[+-]\d{2}:\d{2}$")

    def test_stability_evidence_exports_exact_raw_frames_before_sampling(self):
        stability_samples = (
            MappingProxyType({
                "t": 1_699_999_998.000001,
                "device_timestamp": "2023:11:14 22:13:18 000",
                "cycle": 40,
                "source": "ADT286 SCAN:DATA:Last? 1",
                "ref": 19.999999999,
                "ref_raw": "19.999999999999999999",
                "unit": "°C",
            }),
            MappingProxyType({
                "t": 1_699_999_999.000002,
                "device_timestamp": "2023:11:14 22:13:19 000",
                "cycle": 41,
                "source": "ADT286 SCAN:DATA:Last? 1",
                "ref": 20.000000001,
                "ref_raw": "20.000000000000000001",
                "unit": "°C",
            }),
        )
        engine, adt = export_fixture(
            lambda result: setattr(
                result, "stability_samples", stability_samples))

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stability-and-samples.csv"
            write_samples(engine, adt, str(path))
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.reader(handle))

        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == "Set point requested" and "Phase" in row
        )
        header = rows[header_index]
        evidence_rows = rows[header_index + 1:]

        self.assertEqual([row[header.index("Phase")] for row in evidence_rows],
                         ["stability", "stability", "sampling"])
        self.assertEqual(
            [row[header.index("Reference (°C)")] for row in evidence_rows],
            [
                "19.999999999999999999",
                "20.000000000000000001",
                "20.123456789012345678",
            ],
        )
        self.assertEqual(
            [row[header.index("Scan cycle")] for row in evidence_rows],
            ["40", "41", "42"],
        )
        self.assertEqual(
            [row[header.index("Device acquisition time")]
             for row in evidence_rows],
            [
                "2023:11:14 22:13:18 000",
                "2023:11:14 22:13:19 000",
                "2023:11:14 22:13:20 123",
            ],
        )
        self.assertEqual(
            [row[header.index("DUT (°C)")] for row in evidence_rows],
            ["", "", "20.223456789012345678"],
        )
        for row in evidence_rows:
            self.assertRegex(
                row[header.index("Host receipt time")],
                r"\.\d{6}[+-]\d{2}:\d{2}$",
            )

    def test_export_run_never_reuses_existing_filenames(self):
        engine, adt = export_fixture()
        with tempfile.TemporaryDirectory() as folder:
            first = export_run(engine, adt, folder)
            first_contents = [Path(path).read_bytes() for path in first]
            second = export_run(engine, adt, folder)

            self.assertTrue(all(Path(path).exists() for path in first + second))
            self.assertTrue(set(first).isdisjoint(second))
            self.assertEqual(first_contents, [Path(path).read_bytes() for path in first])
            self.assertTrue(
                all(re.search(r"_2_(summary|samples)\.csv$", path) for path in second)
            )


if __name__ == "__main__":
    unittest.main()
