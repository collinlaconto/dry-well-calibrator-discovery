# Heat Source Discovery Tool (ADT286 companion)

Connect a laptop to any RS-232 heat source and walk away with every value
the Additel ADT286's Temperature Source Management menu asks for — terminator,
set-point writing command, set-point reading command, value command, unit
reading command — each one verified against the live instrument, not copied
from a manual. Works for more than the Fluke 9170: it fingerprints the
protocol and probes candidates, so Fluke/Hart SCPI wells, classic Hart
Scientific 9100-series gear, and generic SCPI instruments resolve
automatically, and anything unknown degrades to a guided manual mode.

## Install (once, on the laptop)

1. Python 3.8+ from python.org (keep the default tcl/tk option checked).
2. `pip install pyserial`
3. A USB-RS232 adapter and the right cable for the heat source.

## Run

```
python heat_source_discovery.py
```

## The tab flow

1. **Instrument** — enter the Make and Serial number (required; the sheet
   records which physical unit the values were verified on). Model, serial,
   and range are pre-filled automatically when discovery can read them.
2. **Connect** — pick the COM port and click **Auto-detect connection**. The
   tool scans baud rates × terminators with read-only probes until the
   instrument answers, reports the protocol family, and flags echo. Manual
   settings are there as a fallback.
3. **Discover** — one click exercises candidate commands against the live
   instrument and adopts what actually works: set-point read, value (block
   temperature), unit (capturing the real unit token for the 286's mapping
   table), then the set-point write, tested with a small delta (default
   0.5°) and restored. Probes are read-only; the enable command is never
   sent, so the well is not driven. Unresolved fields are flagged, never
   guessed.
4. **Review** — every discovered value in an editable form. Gaps can be
   filled by hand with help from the **Terminal** tab.
5. **Sheet** — generates the complete ADT286 entry sheet: instrument
   identity, connection settings, all five command fields, unit mapping,
   range, the raw reply captured as evidence for each verified command, a
   heat-source-side checklist tailored to the protocol family, and the
   286-side steps. Save as .txt or copy to clipboard. Profiles persist to
   `heat_source_profiles.json` for the library sidebar.
6. **Control** — drive the heat source with plain values instead of syntax.
   Pick a profile from the dropdown (saved, quick-picked, or freshly
   discovered), connect with its own settings, type `20` in the set-point
   field and press Send — the profile supplies the command. It reads the
   value back and says whether it took. Buttons read the set point,
   temperature, and unit; a quiet auto-read refreshes every 2 s without
   filling the log.

### Control tab guardrails

Because this tab actually drives the well, it behaves conservatively:

- **Out-of-range values are refused, never clamped.** Entering 600 on a
  −45…140 °C profile sends nothing and tells you why. If a profile has no
  range set, it asks once before sending.
- **Sending a set point never starts heating.** Output enable is a separate
  button with a confirmation prompt, because on most of this gear the output
  resets to off at power-up and must be turned on deliberately.
- **STOP always works**, including while another command is in flight, and
  never asks for confirmation.
- **Readback is the proof.** Every set point is read back and compared; if
  it didn't change, the log names the likely cause (usually a protected set
  point) and the fix. Tick "Send password first" for locked Fluke units.
- Profiles with no enable command (the classic Micro-Baths) say so plainly
  and point you to the front panel rather than sending a guess.

## Notes and limits worth knowing

- **Auto-detect assumes 8-N-1**, which covers essentially all temperature
  calibration equipment; oddballs can be connected manually.
- **Range**: pre-filled from a built-in hint table for common Fluke wells
  (9142/9143/9144, 9170–9173, 9190A) and marked on the sheet as a hint —
  confirm against the nameplate, since the 286 rejects set points outside
  the entered range.
- **Unknown protocols**: the tool tries a battery of common commands; if the
  instrument speaks something proprietary, you'll land in Review + Terminal
  with the discovery log showing exactly what was tried. Add the
  instrument's syntax to the tables at the top of the file
  (`FAMILIES`, `WRITE_PAIRS`, candidate lists) and it becomes a known
  family for everyone after you — that's the intended way to grow it.
- **Generic-SCPI value commands**: when the family isn't pinned, the sheet
  and log remind you to confirm the value command reads the *block*
  temperature rather than a reference input — the one thing a probe can't
  distinguish by itself.
- **Classic Hart syntax** (`s=`, `t`, `u`) is included from protocol
  convention; the live verification step is what proves it on your unit.

## Your fleet, pre-loaded

Every model below is in the built-in knowledge base. Pick it from the
quick-pick on tab 1 (or let auto-detect recognize it from the identity
reply) and the format, range, and checklist load instantly — Discover then
proves it on the live unit.

| Model | Family / commands | Range (°C) |
|---|---|---|
| Fluke 9190A | Fluke SCPI well — `SOUR:SPO`, `SOUR:SPO?`, `SOUR:SENS:DAT? TEMP`, `UNIT:TEMP?`, enable `OUTP:STAT 1` | −95 to 140 |
| Fluke 9170 | same | −45 to 140 |
| Fluke 9173 | same | 50 to 700 |
| Fluke 6109A | Fluke SCPI bath — same but value is `SOUR:SENS:DATA?` (no TEMP arg); replies CR-only unless LF enabled; **null-modem cable** | 35 to 250 |
| Additel 878-160 | Additel — probed live; **check the 286's native Additel driver list first** | −40 to 160 |
| Additel 878-700 | same | 33 to 700 |
| Fluke 7103 | Hart classic — `s=`, `s`, `t`, `u` | −30 to 125 |
| Hart Scientific 7102 | same | −5 to 125 |
| Fluke 6102 | same | 35 to 200 |

(9171/9172, 9142/9143/9144, 7109A, and 878-425 are included too.)

Provenance, honestly stated: the 917X and 6109A/7109A command sets were
verified against Fluke's published manuals; ranges for the fleet come from
the manufacturers' datasheets (confirm on the nameplate — the sheet flags
pre-filled ranges). The classic Micro-Bath syntax (`t`, `s=`, `u`) follows
the long-standing Hart serial convention; Discover's live verification is
the proof on your specific unit. The Additel 878s' remote syntax isn't
embedded because the 286 already ships with **native Additel heat-source
drivers** — check its built-in temperature source list (and update the
286 firmware, which may also add the 9190A/6109A) before building customs;
if you still need one, Discover probes SCPI candidates and Additel's
"Programming Commands for 878" PDF (additel.com/productresources) fills
any gaps via the Review tab.

One power-cycle gotcha shared across the Fluke gear: heat/cool enable
(`OUTP:STAT` / CONT ENABLE) resets to Off — put `OUTP:STAT 1` in the
ADT286's enable/init field or switch it on at the panel before every run.
