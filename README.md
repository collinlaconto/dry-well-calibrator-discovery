# Calibration Automation Suite

Automated multi-point temperature calibration. Your PC drives the heat
sources, an Additel ADT286 does the measuring, and **several calibrations can
run at the same time** — which the 286's own Probe Calibration app cannot do.

At each set point the software waits for the reference probe to settle, takes
a run of samples from the reference and every device under test, records mean,
standard deviation and error, then moves to the next point. When the run
finishes you get a table, a graph, and two CSV files.

Calibration evidence follows a device-only policy: temperature values, units,
scan cycles, timestamps and device identities come from the connected
instruments and are retained without offsets, smoothing, interpolation or unit
conversion. Mean, sample standard deviation, error and verdict are calculated
separately and are always labelled as derived results.

---

## Contents

- [Requirements](#requirements)
- [Install and start](#install-and-start)
- [Quick start](#quick-start)
- [Connecting instruments](#connecting-instruments)
- [Teaching it a heat source's commands](#teaching-it-a-heat-sources-commands)
- [Building a calibration profile](#building-a-calibration-profile)
- [Running calibrations](#running-calibrations)
- [Results and export](#results-and-export)
- [Terminal](#terminal)
- [Supported heat sources](#supported-heat-sources)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Accuracy of the built-in command sets](#accuracy-of-the-built-in-command-sets)

---

## Requirements

- **Python 3.8 or newer**, including tkinter (the GUI toolkit).
  - Windows/macOS: use the installer from python.org and keep the
    **tcl/tk and IDLE** option checked.
  - Debian/Ubuntu: `sudo apt install python3-tk` · Fedora:
    `sudo dnf install python3-tkinter`
- **pyserial**: `pip install pyserial`
- The **Additel USB driver**, if you connect the 286 or a well by USB, so the
  instrument appears as a COM port.

## Install and start

Put all eleven files in one folder, install pyserial once, then
**double-click `Calibration Automation Suite.pyw`**.

```
pip install pyserial

# only for the logger comparison tool:
pip install pandas plotly openpyxl
```

```
your-folder/
    Calibration Automation Suite.pyw   <- double-click this
    run_calibration_suite.py           <- same app, from a command prompt
    ui.py        theme.py       engine.py     adt286.py
    heatsource.py               transport.py  formats.py    export.py
    datasync.py
```

The `.pyw` extension tells Windows to run it with `pythonw.exe`, so the window
opens on its own with **no console behind it**. If you start the `.py` version
instead, the console it creates is hidden automatically — but a prompt you
already had open is left alone, so running it from a terminal still shows
output.

The launcher works whether the modules sit flat beside it or in a `calsuite/`
subfolder. If a file is missing it says which in a dialog box, rather than
failing silently behind a hidden console.

The window title and first Activity entry show the build identifier and the
folder actually loaded. This matters when several downloaded folders contain
the same launcher name. The first non-empty ADT286 parser input for each
channel set is also copied exactly once into Activity with a parse summary,
making a firmware-format problem diagnosable without changing any device
value.

Two files are created next to the launcher and grow as you work:

| File | Holds |
|---|---|
| `heat_source_profiles.json` | each heat source: connection, range, command set, unit |
| `calibration_profiles.json` | each calibration: channels, set points, stability and sampling settings |

## Logger comparison

A second tool in the suite: plot data loggers against a reference probe on one
time axis, to see how each device tracked during a soak.

It reads almost any logger export (CSV, TXT, XLSX) with no per-brand setup.
Rather than matching one brand's exact headings, it analyses which column
parses as timestamps or elapsed time and scores temperature columns by their
headings and data shape. Metadata preambles and summary blocks above the real
header are skipped automatically. Every selection is shown and can be
overridden.

**Units are explicit.** A unit is accepted from a column heading or from the
operator's unit selection. Numeric magnitude is never used to guess a unit: an
unlabelled value of 100 could be 100 °C or 100 °F. Files with no trustworthy
unit stop for confirmation. When a comparison display uses °C, conversion is
performed on a copy for the chart; the imported source values and units remain
unchanged.

**The reference can be a calibration run from this application**, which skips
an export-and-reimport round trip and guarantees the comparison is against the
same readings the results were built from. A probe file works too; if it
records elapsed time, you will be asked for the start time. Run-derived traces
use each selected channel's ADT286 acquisition timestamp, never the PC receipt
time, so transport delay cannot shift a reference or DUT trace.

Loggers are trimmed to the reference's time window, and anything that does not
overlap it is called out rather than silently plotted. A reference alone, or
loggers alone, can be charted too. Files recording elapsed time need a start
time, which applies to the reference and the loggers alike. Output is a
self-contained interactive HTML chart.

Needs `pandas`, `plotly` and `openpyxl`. Without them this page explains what
to install and the rest of the application works normally.

## The interface

A light, compact calibration workspace uses a slim navy navigation rail and
clear workflow groups, inspired by the structure of commercial calibration
systems while retaining its own visual identity. The primary path—Instruments,
Profiles, Runs and Results—appears first; comparison and service tools sit under
Advanced. Device evidence and derived calculations are visibly separated.
Measured values use tabular figures so decimal points line up, with green for
in tolerance, red for out of tolerance and amber for waiting or invalid data.

The **Runs** page puts the calibration you are watching at the top, at a size
readable from across the bench:

- **Large readouts** — the live reference in cyan, the set point, and points
  completed.
- **A stabilisation curve** — the reference probe's recent history with the
  stability band drawn around the running mean, so you can watch the trace
  settle into it instead of guessing how long is left.
- **A deviation band per device** — the tolerance envelope around zero with a
  dot for the current error, and a PASS or FAIL beside it. Whether a probe is
  failing is legible without reading a number.

Underneath, every other run gets one compact strip with the same information
in miniature. Click a strip to bring that run into focus. Below that, the
selected run's channels are listed with reading age, so a stalled scan is
obvious.

## Quick start

1. **Instruments** — connect the ADT286, then each heat source.
2. Select a heat source and press **Check / discover commands** (once per
   instrument, ever).
3. **Profiles** — pick the heat source, the reference probe's channel, the DUT
   channels, and type your set points. Press **Check this profile**.
4. **Runs** — start it. Watch live readings; start another run on another heat
   source if you want.
5. **Results** — read the table and graph, then **Export CSV**.

---

## Connecting instruments

Every instrument is reached through a *connection target*, so its command set
is independent of the cable:

| Connection | What to enter | Notes |
|---|---|---|
| USB / serial | COM port and baud | Needs the Additel USB driver for Additel gear |
| Ethernet / Wi-Fi | IP address and port | **Additel devices use port 8000** (the default here) |
| Bluetooth | the COM port that pairing created | Pair in the OS first; Windows exposes an outgoing COM port |

**Find port** — if you don't know an instrument's socket port, type the IP and
press it. It asks each likely port (8000 first, then 5025, 5000, 8080, 8888,
2000, 23, 1024, 10001) for its identity and fills in whichever answers
`*IDN?`. Ports that are open but silent are ignored, so a stray web server
can't be mistaken for the instrument.

**Bluetooth** — pairing a Serial Port Profile device creates a virtual COM
port, and that is the reliable route on Windows. A raw RFCOMM socket (entering
a Bluetooth address instead of a port) works on Linux only; elsewhere the app
says so rather than failing obscurely. A device that only exposes Bluetooth
Low Energy to a phone app is not reachable this way — use Wi-Fi or Ethernet.

**Several identical wells** are told apart by their *connection*, not their
model name. The first connects as "Additel 878-160"; a second at a different
address becomes "Additel 878-160 SN «serial»", or "@ «address»" if it reports
no serial. Only the *same address* twice is refused, and it names the source
already using it.

## Teaching it a heat source's commands

Different manufacturers use quite different remote command sets, so rather
than assume, the software proves them against the instrument.

Connect the heat source, select it in the table, and press **Check / discover
commands**. It tries candidate commands for the set point, temperature, unit
and heat/cool control, keeps whichever the instrument actually answers, and
confirms the set-point write by changing it slightly and reading it back
before restoring the original value. The heat/cool command is found from its
read-only query form and is **never actuated**. Whatever it proves is saved,
so this is a one-time step per instrument.

Two details it handles for you:

- **Verification by error queue.** A write command returns nothing, so its
  reply proves nothing. Where an instrument keeps an error queue, each
  candidate is confirmed with `SYSTem:ERRor?` — `0` means accepted, `-110`
  means the header wasn't recognised. The queue is cleared immediately before
  each checked command, so an older error cannot be blamed on a later command.
  Instruments without an error queue fall back to judging by replies.
- **Set points that need a unit.** Additel wells report the set point as value
  *and* unit (`60.0000,1001`) and require the unit back on a write; sent
  without it they answer `-109 Missing parameter` and nothing changes. The
  write template carries a `{unit}` placeholder:

  ```
  TEMPerature:TARGet {value},{unit}   ->   TEMPerature:TARGet 60.00,1001
  ```

  The unit is never hard-coded. It is taken from the instrument's own reply
  and refreshed on every read, so a well switched to °F simply starts
  reporting `1002` and that is what gets sent.

If a command can't be found automatically, the [Terminal](#terminal) will find
it or let you enter it by hand.

## Building a calibration profile

On the **Profiles** tab:

| Setting | Meaning | Default |
|---|---|---|
| Tolerance | how far a device may sit from the reference and still pass | ±0.05 |
| Heat source | which connected instrument drives the temperature | — |
| Reference probe channel | the 286 channel your reference is on | — |
| DUT channels | the channels being calibrated (multi-select) | — |
| Set points | in the order they run, e.g. `0, 50, 100, 50, 0` | — |
| Stability band | peak-to-peak the reference may move | 0.02 |
| Window it must hold | how long it must stay that flat | 60 s |
| Set-point tolerance | how close the reference must be to the requested point | ±1.0 |
| Give up after | maximum wait for one point | 2400 s |
| Soak after stable | extra dwell before sampling | 0 s |
| Samples per set point | readings taken at each point | 10 |
| Seconds between samples | interval between them | 5 s |
| Enable output at start / off at end | whether to drive the heater remotely | on / on |
| If a point never stabilises | `record` (flag it) or `abort` | record |

### Tolerance

Choose **one tolerance for the whole range**, or **a tolerance for each set
point** — useful when a specification tightens at ambient and loosens at the
extremes. "Fill from the single value" turns one number into a per-point list
you can then edit. Give one value per set point, in the same order; a
mismatched count is caught before the run starts.

Every recorded point carries the tolerance that applied to it, each device is
marked PASS or FAIL against it, and the summary CSV states an overall verdict
for the run. If no tolerance is set, results are left blank rather than
quietly marked as passing.

**Check this profile** validates everything before any hardware moves: channel
assignments, duplicate or clashing channels, and whether every set point is
inside that heat source's range. It also checks the stability settings against
the 286's scan rate (see below).

A channel can belong to only one running calibration at a time; this is
enforced when a run starts.

## Running calibrations

Start as many runs as you have heat sources — one calibration per heat source.
The **Runs** tab shows each run's phase, current set point, points completed,
**the live reference reading and every DUT reading**. Select a run to open a
per-channel panel underneath showing the reference and each device with its
reading, its live error against the reference, and how old the reading is.

> The live error is for monitoring only. Recorded results are the means over
> the samples taken at each set point, after stability is met.

**STOP EVERYTHING** halts all runs and switches outputs off where the profile
allows.

### What "stable" means

The software watches the **reference probe** through the 286 — not the well's
internal sensor. A point is stable once it has been watched for at least the
window, the most recent window of readings is flat within the band, and its
mean is within the required set-point tolerance. A flat reference at the wrong
temperature can never receive a valid verdict.

A point that never stabilises is, by default, sampled anyway and marked
**NOT VALID** in the table and CSV. Its device readings are preserved for
traceability, but neither the point nor the overall run can receive PASS.
Choose `abort` if you'd rather the run stop immediately.

### Using the 286 by hand during a run

You can. The 286 keeps a single scan configuration, so switching its display
to another function cancels the scan and readings stop. The software watches
for this: after three polls with no data — or with subscribed channels missing
— it re-sends the scan command and carries on. Recovery is rate-limited so it
can't become a restart storm, it's announced in the Activity log, and the Runs
tab shows live scan health ("scanning", "no data — recovering",
"recovered 2×"). Runs survive the interruption; a stability window just takes
a little longer to fill. Reading age in the live panel is the honest indicator
that something was interrupted.

### Scan rate and stability windows

**Read channels every** on the Instruments tab (1 / 2 / 5 / 10 s) sets how
often the 286 is queried. The application never requests readings faster than
the device's configured one-second scan. It requests the ADT286's own
millisecond acquisition timestamp with every frame. When the firmware returns
it, a repeated timestamp is withheld rather than assigned a new software
sample. Some ADT286 firmware omits this optional field even when requested; in
that case the complete device readings are retained, the device-time field is
left blank, and host receipt time is stored separately rather than fabricated
as device time. The reply timeout is measured from the most recently received
bytes, so a large multi-channel frame is not cut off merely because its total
serial transfer takes more than two seconds.

There's a coupling worth knowing: stability needs **at least three readings
inside the window**, so a window shorter than three read intervals can never
be satisfied — every set point would time out even with a perfectly steady
bath. "Check this profile" catches this before a run and says what to use;
changing the read interval reports the new minimum and warns if a running
profile's window has become too short. The same applies to a sample interval
faster than the scan.

## Results and export

The **Results** tab has two explicit views:

- **Device readings — immutable**: the exact numeric tokens and acquisition
  timestamps returned by the ADT286, host receipt time, device scan cycle,
  source command and phase.
- **Derived summary**: reference and DUT mean, sample standard deviation,
  error, tolerance, sample count, stability state and verdict, plus an error
  graph. One observation has no standard deviation; it is left blank rather
  than reported as zero.

**Export CSV** writes two files into a folder you choose:

- `«run»_«timestamp»_summary.csv` — derived statistics, tolerance and verdict
- `«run»_«timestamp»_samples.csv` — unrounded device readings and stability
  evidence, with fractional-second timestamps and scan cycles

Both begin with the identities, connections, units, ranges, channel
assignments and acquisition settings pinned when the run started. Exporting
after reconnecting another device cannot relabel an earlier run. Existing
filenames are never overwritten, and a run cannot be exported while its worker
is still changing the evidence set. A validated profile, every completed point
and all nested sample evidence are sealed read-only; the two CSVs are staged
and published as one export operation, with rollback if either file fails.

The heat source's set-point command, exact readback text and live reported unit
are also retained. They are checked after the command, around every sample and
after sampling. The unit token attached to the set-point reply and the separate
live unit query are recorded in distinct fields with query/validation status;
a conflict, unknown token or failed query invalidates the point without
relabeling it. That failed device evidence is still sealed and exported even
when the failure occurs before the first ADT286 sample.

## Terminal

For hands-on work with any connected instrument, over whichever connection it
uses. It is query-only by default. Device-changing writes require an explicit
Service mode and are blocked while that device belongs to an active
calibration.

- **Read set point / Read temperature / Read unit** send whichever command
  *that* instrument uses, taken from its own profile — so they work on an
  Additel well and a Fluke well alike, and say so plainly if a command hasn't
  been found yet.
- **Find … command** sweeps every known form, reports each verdict, and saves
  the one that answers.
- Type any command directly to test it. With the error-queue box ticked, each
  command is followed by `SYSTem:ERRor?` and the verdict shown: accepted, or
  rejected with the instrument's own error text.

## Supported heat sources

Selecting one of these pre-fills its range and, where known, its command set.
Anything else can be added by connecting it and running discovery.

| Model | Type | Range (°C) |
|---|---|---|
| Fluke 9190A | Ultra-cool field metrology well | −95 to 140 |
| Fluke 9170 / 9171 | Field metrology well | −45 to 140 / −30 to 155 |
| Fluke 9172 / 9173 | Metrology well | 35 to 425 / 50 to 700 |
| Fluke 9142 / 9143 / 9144 | Field metrology well | −25 to 150 / 33 to 350 / 50 to 660 |
| Fluke 6109A / 7109A | Portable calibration bath | 35 to 250 / −25 to 140 |
| Additel 878-160 / 878-425 / 878-700 | Reference dry well | −40 to 160 / 33 to 425 / 33 to 700 |
| Fluke 7102 / 7103 / 6102 | Micro-bath | −5 to 125 / −30 to 125 / 35 to 200 |

Ranges come from the manufacturers' datasheets — confirm against the
nameplate, since the software refuses set points outside the range you set.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "No module named calsuite" | A file is missing or misplaced. Put all eleven files in one folder; the launcher names anything absent in a dialog. |
| A console window sits behind the app | Start `Calibration Automation Suite.pyw`. If one still appears, Windows may be opening `.pyw` files with `python.exe`: right-click the file, Open with, and choose `pythonw.exe`. The `.py` launcher also hides its own console and, failing that, restarts itself windowless. Set `CALSUITE_KEEP_CONSOLE=1` to keep the console for debugging. |
| Readings stop mid-run | The 286's display was changed, cancelling the scan. It recovers automatically within a few seconds — watch the scan health indicator. |
| Every set point times out, but the bath looks steady | The stability window is too short for the scan rate. Use at least three read intervals, or scan faster. |
| No set-point command recognised | Run **Check / discover commands**. If it still fails, use the Terminal's **Find set-point command**, then type candidates by hand. |
| `-109 Missing parameter` on a write | The instrument needs the unit with the value. Re-run discovery; it will adopt the `{unit}` form. |
| `-110 Command header error` | Wrong command for this instrument — the dialect differs by manufacturer. |
| Set point won't change | It may be locked on the instrument. Tick "Send password before set points", or unlock it on the panel. |
| Second identical well won't connect | Only the same *address* is refused. Check you changed the IP or port. |
| Well never heats | Micro-baths have no remote output enable; switch it on at the front panel. Fluke wells reset their output to off at power-up. |
| Nothing answers over the network | Check the IP, that the network interface is on, and press **Find port**. |

## How it works

The 286's built-in Probe Calibration app runs one profile at a time. This
software inverts the arrangement: the PC owns the sequencing and drives each
heat source directly, using the 286 purely as the readout. The heat sources
never need to be presets on the 286 at all.

The one real constraint is that **the 286 holds a single scan configuration**,
so concurrent runs can't each start their own scan. Every run subscribes its
channels to one shared scan, and readings are fanned out by channel name — one
poll serves everyone. That is why channels are locked to a run while it is
active, why starting or finishing a run briefly reconfigures the scan, and why
the live readings on the Runs tab cost nothing extra.

The software never changes the 286's channel setup or its units. Those are
global, so altering them mid-flight would corrupt another run's data — set
sensor types on the instrument beforehand. Channels are shown with their
configured type so you can confirm assignments.

## Accuracy of the built-in command sets

- **ADT286** — from Additel's published command set (`MODule:INFormation?`,
  `MODule:CONFig?`, `SCAN:MULT:STARt`, `SCAN:DATA:Last? 1`, `SCAN:STOP`,
  `UNIT:TEMPerature?`). The scan-data parser is tested against Additel's own
  worked example and tolerates the longer thermocouple form with cold-junction
  fields appended.
- **Fluke 917X wells and 6109A/7109A baths** — from their manuals.
- **Hart Scientific micro-baths** (`t`, `s=`, `u`) — from long-standing
  convention, not a manual.
- **Additel wells** — inferred from Additel's house style in their published
  command sets, where the root keyword is the quantity itself
  (`TEMPerature:TARGet`, not `SOURce:SPOint`). **Not** copied from the 878
  document.

The last two are exactly why discovery exists: run **Check / discover
commands** and let the instrument settle it before trusting a run to it. The
authoritative lists are the "Programming Commands" PDFs on Additel's Product
Resources page, and the manuals for Fluke gear.

The software has been tested end-to-end against simulated instruments —
including concurrent runs sharing one 286, a well driven over a real TCP
socket, an instrument that requires the unit ID, and a 286 whose scan is
cancelled mid-run — but not against your bench. The Activity log records every
exchange, so anything unexpected is diagnosable.
