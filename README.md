# Temperature Calibration Suite

Automated multi-point temperature calibration: the PC drives the heat sources,
the ADT286 does the measuring, and several calibrations can run at once.

```
pip install pyserial
python run_calibration_suite.py
```

`heat_source_discovery.py` (the standalone format-discovery tool) still runs on
its own and writes `heat_source_profiles.json`. **Keep both in the same folder** —
the suite reads that same library, so a heat source you verify once shows up
ready to use.

## Why this gets around the 286's one-profile limit

The 286's built-in Probe Calibration app runs one profile at a time. This suite
inverts the arrangement: the PC owns the sequencing and drives each heat source
directly over its own serial port, while the 286 is used purely as the readout.
The heat sources never need to be presets on the 286 at all, which also sidesteps
the Temperature Source Management gap entirely.

**The one real constraint** is that the 286 has a single scan configuration —
`SCAN:MULT:STARt` takes one channel list, so concurrent runs cannot each start
their own scan. The suite handles this by having every run *subscribe* its
channels to one shared scan; readings are then fanned out by channel name. One
poll serves everyone. Consequences worth knowing:

- **Channels are locked to a run while it is active.** A `ChannelRegistry`
  refuses to start a run whose reference or DUT channel is already claimed, so
  two runs can never read each other's probes.
- **Starting or finishing a run reconfigures the shared scan**, which pauses
  data for about a second. Harmless — the engine waits for fresh scan cycles —
  but it is why the scan restarts appear in the log.
- **The suite never changes the 286's channel setup or units.** Those are
  global, so changing them mid-flight would corrupt another run's data. Set
  sensor types on the instrument beforehand; the suite reads and displays them
  so you can confirm assignments.

## Workflow

1. **Instruments** — connect the 286 over USB (Additel USB driver → virtual COM
   port). Channels are enumerated from the module inventory and shown with
   their configured sensor type. Then connect each heat source on its own port,
   picking either a saved profile or a built-in model format.
2. **Profiles** — name the run, pick the heat source, pick the reference probe's
   channel, multi-select the DUT channels, and enter set points in the order you
   want them (e.g. `0, 50, 100, 50, 0` for an up-and-down sequence). Set the
   stability band and window, samples per point, and interval. "Check this
   profile" validates everything — including that every set point is inside the
   heat source's range — before any hardware moves.
3. **Runs** — start as many as you have heat sources for. The table shows each
   run's phase, current set point, points completed, and live reference reading.
   STOP EVERYTHING stops all runs and switches outputs off where the profile
   allows it.
4. **Results** — table of every set point × DUT channel with reference mean,
   DUT mean, standard deviations, and error; graph of error against set point
   per channel with a zero line. Export writes two CSVs: a summary (the
   calibration table) and every raw sample, both with full run metadata.

## What "stable" means here

Per set point the engine sends the set point, confirms it by readback, then
watches the **reference probe** (read through the 286, not the well's internal
sensor). A point is stable when it has been watched for at least the window and
the most recent window of readings is flat within the band. If a point never
stabilises within "give up after", the default is to sample anyway and mark the
result **NOT STABLE** — visible in the table and as `NO` in the CSV — so a long
run isn't lost but bad data can never masquerade as good. Set it to `abort`
instead if you'd rather the run stop.

## Verified against the documentation

The 286 commands come from Additel's published command set: `MODule:INFormation?`,
`MODule:CONFig?`, `SCAN:MULT:STARt`, `SCAN:DATA:Last?`, `SCAN:STOP`,
`UNIT:TEMPerature?`. The scan-data parser is tested against Additel's own worked
example (`REF1,1281,1,28.258167,28.258167,1001,1,33.512077;` → 33.512077 °C) and
tolerates the longer thermocouple form with cold-junction blocks appended.
Fluke 917X and 6109A/7109A command sets are from their manuals. The classic
Micro-Bath syntax (`t`, `s=`, `u`) is convention-derived — verify it with the
discovery tool before trusting a run to it.

## Limits worth knowing before the first real run

- **Micro-Baths have no remote output enable.** The suite detects this, warns
  clearly, and relies on the front panel — it will not silently assume the bath
  is heating.
- **One calibration per heat source.** Enforced.
- **Sampling rate is bounded by the scan poll** (1 s default), so intervals
  below that will not produce more distinct samples.
- **Ranges are pre-filled from datasheets** — confirm against the nameplate.
- Tested end-to-end against a simulated bench (two wells, one 286, concurrent
  runs), not yet against your hardware. The first real run is the real test:
  the Activity log records every exchange, so anything surprising is diagnosable.
