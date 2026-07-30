# Temperature Calibration Suite

Automated multi-point temperature calibration: the PC drives the heat sources,
the ADT286 does the measuring, and several calibrations can run at once —
over USB, Ethernet, Wi-Fi, or Bluetooth.

## Install and run

Put **all nine .py files in one folder** — no subfolders needed — then:

```
pip install pyserial
python run_calibration_suite.py
```

```
your-folder/
    run_calibration_suite.py     <- start this one
    ui.py  engine.py  adt286.py  heatsource.py
    transport.py  formats.py  export.py
    heat_source_discovery.py     <- standalone serial format tool
```

The launcher accepts either this flat layout or a `calsuite/` subfolder. If
files are missing it names them instead of throwing an import error. Profile
libraries (`heat_source_profiles.json`, `calibration_profiles.json`) are
written next to the launcher, which is how the discovery tool and the suite
share heat-source formats.

Python needs tkinter for the window: it ships with the python.org installers
(keep "tcl/tk and IDLE" checked); on Debian/Ubuntu `sudo apt install
python3-tk`.

## Connections: USB, Ethernet, Wi-Fi, Bluetooth

Every instrument — the 286 and each heat source — is reached through a
**connection target**, so an instrument's command set is independent of how
it is plugged in.

| Choice | What to enter | Notes |
|---|---|---|
| USB / serial cable | COM port + baud | Install Additel's USB driver so the unit appears as a COM port |
| Network (Ethernet / Wi-Fi) | IP address + socket port | **Find port** discovers the port for you |
| Bluetooth (paired SPP port) | the COM port pairing created | Pair in the OS first; Windows exposes an outgoing COM port |

**If you don't know the socket port**, type the IP and press **Find port**. It
asks each likely port (5025 first — the IANA "scpi-raw" port — then 5000,
8080, 8888, 2000, 23, 1024, 10001) for its identity and fills in whichever
answers `*IDN?`. Ports that are open but silent are ignored, so a stray web
server cannot be mistaken for the instrument.

**Bluetooth**: pairing a Serial Port Profile device creates a virtual COM
port, and that is the reliable route on Windows — a Bluetooth target with a
COM port is sent straight to the serial transport. A raw RFCOMM socket
(entering a Bluetooth address instead) works on Linux only; elsewhere the app
says so and points at the paired-port route. If a unit only exposes Bluetooth
Low Energy to Additel's phone app, it is not reachable this way — use Wi-Fi or
Ethernet.

## The Additel 878-160 and 878-700

These default to a **network** connection when added, with ranges pre-filled
(-40 to 160 °C and 33 to 700 °C).

Their command syntax is deliberately **left blank**, because I could not
verify Additel's exact SCPI wording from the documentation — and a wrong
command sitting silently in a profile is worse than an empty one. Instead:

1. Connect the well on the Instruments tab.
2. Select it in the table and press **Check / discover commands**.

That probes candidate commands live, adopts whatever the instrument actually
answers to, and confirms the set-point write by changing it slightly and
reading it back — then restores the original value. The output enable is
never sent. Whatever it proves is saved to `heat_source_profiles.json`, so it
is a one-time step per instrument. Anything it cannot find is named plainly,
with the Activity log showing every command tried, so you can fill the gap
from Additel's "Programming Commands for 878" PDF.

## Why this gets around the 286's one-profile limit

The 286's built-in Probe Calibration app runs one profile at a time. This
suite inverts the arrangement: the PC owns the sequencing and drives each heat
source directly, while the 286 is used purely as the readout. The heat sources
never need to be presets on the 286 at all, which also sidesteps the
Temperature Source Management gap entirely.

**The one real constraint** is that the 286 has a single scan configuration —
`SCAN:MULT:STARt` takes one channel list, so concurrent runs cannot each start
their own scan. Every run *subscribes* its channels to one shared scan and
readings are fanned out by channel name; one poll serves everyone.
Consequences:

- **Channels are locked to a run while it is active.** A `ChannelRegistry`
  refuses to start a run whose reference or DUT channel is already claimed.
- **Starting or finishing a run reconfigures the shared scan**, pausing data
  for about a second. Harmless — the engine waits for fresh scan cycles.
- **The suite never changes the 286's channel setup or units.** Those are
  global, so changing them mid-flight would corrupt another run's data. Set
  sensor types on the instrument beforehand; the suite reads and displays them
  so you can confirm assignments.

## Workflow

1. **Instruments** — connect the 286, then each heat source on its own
   connection. Channels are enumerated from the module inventory and shown
   with their configured sensor type.
2. **Profiles** — name the run, pick the heat source, the reference probe's
   channel, and the DUT channels; enter set points in order (e.g.
   `0, 50, 100, 50, 0`). Set the stability band and window, samples per point,
   and interval. "Check this profile" validates everything — including that
   every set point is inside the heat source's range — before hardware moves.
3. **Runs** — start as many as you have heat sources for. The table shows each
   run's phase, current set point, points completed, and live reference
   reading. STOP EVERYTHING halts all runs and switches outputs off where the
   profile allows.
4. **Results** — table of every set point x DUT channel with reference mean,
   DUT mean, standard deviations and error; graph of error against set point
   per channel. Export writes two CSVs: a summary (the calibration table) and
   every raw sample, both with full run metadata.

## What "stable" means here

Per set point the engine sends the set point, confirms it by readback, then
watches the **reference probe** (read through the 286, not the well's internal
sensor). A point is stable when it has been watched for at least the window
and the most recent window of readings is flat within the band. If a point
never stabilises within "give up after", the default is to sample anyway and
mark the result **NOT STABLE** — visible in the table and as `NO` in the CSV —
so a long run is not lost but bad data cannot masquerade as good. Set it to
`abort` if you would rather the run stop.

## Verified against the documentation

The 286 commands come from Additel's published command set:
`MODule:INFormation?`, `MODule:CONFig?`, `SCAN:MULT:STARt`,
`SCAN:DATA:Last?`, `SCAN:STOP`, `UNIT:TEMPerature?`. The scan-data parser is
tested against Additel's own worked example
(`REF1,1281,1,28.258167,28.258167,1001,1,33.512077;` -> 33.512077 °C) and
tolerates the longer thermocouple form with cold-junction blocks appended.
Fluke 917X and 6109A/7109A command sets are from their manuals. The classic
Micro-Bath syntax (`t`, `s=`, `u`) is convention-derived, and the Additel 878
syntax is left to live discovery — verify both before trusting a run to them.

## Limits worth knowing before the first real run

- **Micro-Baths have no remote output enable.** The suite detects this, warns
  clearly, and relies on the front panel rather than assuming the bath heats.
- **One calibration per heat source.** Enforced.
- **Sampling rate is bounded by the scan poll** (1 s default).
- **Ranges are pre-filled from datasheets** — confirm against the nameplate.
- Tested end-to-end against simulated instruments — two wells sharing one 286
  over serial, and a full run driving a well over a real TCP socket — but not
  yet against your hardware. The Activity log records every exchange, so
  anything surprising is diagnosable.
