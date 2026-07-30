"""Heat-source command formats and model knowledge base.

Verified sources:
  * Fluke 917X Field Metrology Wells  — SCPI-1994 command set
  * Fluke 6109A/7109A Calibration Baths — SCPI 1999.0 (operators manual)
Convention-derived (proved by live verification before use):
  * Hart Scientific classic serial (Micro-Bath 6102/7102/7103)
"""

import re

from .transport import DEFAULT_TCP_PORT

TERMINATORS = {"CRLF": "\r\n", "CR": "\r", "LF": "\n"}

FAMILIES = {
    "fluke_scpi": {
        "label": "Fluke/Hart SCPI metrology well (914X / 917X / 9190A)",
        "commands": {
            "sp_read": "SOUR:SPO?",
            "sp_write": "SOUR:SPO {value}",
            "value": "SOUR:SENS:DAT? TEMP",
            "unit": "UNIT:TEMP?",
        },
        "value_alts": ["SOUR:SENS:DATA?"],
        "enable": "OUTP:STAT 1",
        "disable": "OUTP:STAT 0",
        "password": "SYST:PASS:CEN 1234",
        "checklist": [
            "ECHO Off in the COMM menu",
            "PRINT / serial-period streaming Off",
            "CONT ENABLE resets to Off at power-up (remote: OUTP:STAT 1)",
            "SETPOINT PROT may block writes; default password 1234",
        ],
    },
    "fluke_bath": {
        "label": "Fluke 6109A/7109A Portable Calibration Bath (SCPI 1999.0)",
        "commands": {
            "sp_read": "SOUR:SPO?",
            "sp_write": "SOUR:SPO {value}",
            "value": "SOUR:SENS:DATA?",
            "unit": "UNIT:TEMP?",
        },
        "value_alts": ["SOUR:SENS:DAT? TEMP"],
        "enable": "OUTP:STAT 1",
        "disable": "OUTP:STAT 0",
        "password": "SYST:PASS:CEN 1234",
        "checklist": [
            "Temperature control (OUTP:STAT) is OFF after power-up and *RST",
            "Setup > Instrument > Remote > Termination must match",
            "Serial Monitor (auto-streaming) OFF",
            "RS-232 is DTE: use a NULL-MODEM cable; 8-N-1",
        ],
    },
    "hart_classic": {
        "label": "Hart Scientific classic serial (Micro-Bath 6102/7102/7103)",
        "commands": {
            "sp_read": "s",
            "sp_write": "s={value}",
            "value": "t",
            "unit": "u",
        },
        "value_alts": [],
        "enable": "",
        "disable": "",
        "password": "",
        "checklist": [
            "Turn echo Off if replies repeat the command",
            "Sample period / auto-print Off",
            "Syntax follows Hart convention — verify live before trusting",
        ],
    },
    "additel_well": {
        "label": "Additel dry well / bath (875 / 878 series)",
        # Left empty deliberately: Additel's published 878 command list was not
        # available to confirm, so nothing is asserted here. Use "Verify
        # commands" and the instrument itself decides what works.
        "commands": {},
        "value_alts": [],
        "enable": "OUTP:STAT 1",
        "disable": "OUTP:STAT 0",
        "password": "",
        "probe": True,
        "checklist": [
            "Run 'Verify commands' once — the syntax below is adopted from "
            "what the instrument actually answers, not assumed",
            "The ADT286 also ships native Additel drivers, useful as a "
            "cross-check",
            "Authoritative syntax: 'Programming Commands for 878' at "
            "additel.com/productresources",
            "On Wi-Fi/Ethernet, read the IP off the instrument's network "
            "screen; use 'Find port' if the socket port is unknown",
        ],
    },
    "generic_scpi": {
        "label": "Generic SCPI instrument",
        "commands": {},
        "value_alts": [],
        "enable": "",
        "disable": "",
        "password": "",
        "checklist": ["Confirm the value command reads the BLOCK temperature"],
    },
    "unknown": {
        "label": "Unknown protocol (manual entry)",
        "commands": {},
        "value_alts": [],
        "enable": "",
        "disable": "",
        "password": "",
        "checklist": ["Use the discovery tool to find working commands"],
    },
}

# token -> (family, low °C, high °C, description)
KNOWN_MODELS = {
    "9190":    ("fluke_scpi",   -95, 140, "Ultra-Cool Field Metrology Well"),
    "9170":    ("fluke_scpi",   -45, 140, "Field Metrology Well"),
    "9171":    ("fluke_scpi",   -30, 155, "Field Metrology Well"),
    "9172":    ("fluke_scpi",    35, 425, "Field Metrology Well"),
    "9173":    ("fluke_scpi",    50, 700, "Metrology Well"),
    "9142":    ("fluke_scpi",   -25, 150, "Field Metrology Well"),
    "9143":    ("fluke_scpi",    33, 350, "Field Metrology Well"),
    "9144":    ("fluke_scpi",    50, 660, "Field Metrology Well"),
    "6109":    ("fluke_bath",    35, 250, "Portable Calibration Bath"),
    "7109":    ("fluke_bath",   -25, 140, "Portable Calibration Bath"),
    "878-160": ("additel_well", -40, 160, "Reference Dry Well"),
    "878-425": ("additel_well",  33, 425, "Reference Dry Well"),
    "878-700": ("additel_well",  33, 700, "Reference Dry Well"),
    "7102":    ("hart_classic",  -5, 125, "Micro-Bath"),
    "7103":    ("hart_classic", -30, 125, "Micro-Bath"),
    "6102":    ("hart_classic",  35, 200, "Micro-Bath"),
}


# Candidates tried by HeatSource.verify_commands when a profile has no
# proven command set. Ordered most-likely-first. Nothing here is asserted to
# be correct for any particular instrument -- the point is to find out which
# one it actually answers.
SP_READ_CANDIDATES = ("SOUR:SPO?", "SOUR:SPOint?", "SOUR:TEMP:SPO?",
                      "SOUR:SPO:TEMP?", "SETP?", "SP?", "s")
VALUE_CANDIDATES = ("SOUR:SENS:DAT? TEMP", "SOUR:SENS:DATA?", "MEAS:TEMP?",
                    "MEAS?", "SOUR:TEMP?", "t")
UNIT_CANDIDATES = ("UNIT:TEMP?", "UNIT:TEMPerature?", "u")
ENABLE_CANDIDATES = ("OUTP:STAT 1", "OUTP:STATe ON")

# A verified set-point read implies its paired write command.
WRITE_PAIRS = {
    "SOUR:SPO?": "SOUR:SPO {value}",
    "SOUR:SPOint?": "SOUR:SPOint {value}",
    "SOUR:TEMP:SPO?": "SOUR:TEMP:SPO {value}",
    "SOUR:SPO:TEMP?": "SOUR:SPO:TEMP {value}",
    "SETP?": "SETP {value}",
    "SP?": "SP {value}",
    "s": "s={value}",
}


def first_float(text):
    """First numeric value in a reply, or None."""
    if not text:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    return float(m.group()) if m else None


def model_info(text):
    """Match a known model token inside free text."""
    up = (text or "").upper()
    for token, (family, lo, hi, kind) in KNOWN_MODELS.items():
        if token in up:
            return {"token": token, "family": family, "lo": lo, "hi": hi,
                    "kind": kind}
    return None


# Models that are typically reached over the network rather than a cable.
NETWORK_FIRST_MODELS = ("878-160", "878-425", "878-700")


def default_target(token):
    """Suggested connection for a model: network first where that is usual."""
    if any(token.startswith(m) or m in token for m in NETWORK_FIRST_MODELS):
        return {"kind": "tcp", "host": "", "tcp_port": 5025}
    return {"kind": "serial", "port": "", "baud": "9600"}


def profile_for_model(token, make=None, sn=""):
    """Build a ready-to-use heat-source profile from a known model token."""
    info = model_info(token)
    if not info:
        return None
    fam = FAMILIES[info["family"]]
    cmds = fam.get("commands") or {}
    return {
        "name": f"{make or ('Additel' if token.startswith('878') else 'Fluke')}"
                f" {token}" + (f" SN {sn}" if sn else ""),
        "make": make or ("Additel" if token.startswith("878") else "Fluke"),
        "model": token,
        "sn": sn,
        "kind": info["kind"],
        "family": info["family"],
        "range_min": info["lo"],
        "range_max": info["hi"],
        "range_unit": "°C",
        "baud": "9600",
        "terminator_name": "CRLF",
        "target": default_target(token),
        "sp_write": cmds.get("sp_write", ""),
        "sp_read": cmds.get("sp_read", ""),
        "value": cmds.get("value", ""),
        "unit": cmds.get("unit", ""),
        "enable": fam.get("enable", ""),
        "disable": fam.get("disable", ""),
        "password": fam.get("password", ""),
        "verified": False,
    }


def classify_family(idn):
    up = (idn or "").upper()
    if not up:
        return "unknown"
    if "ADDITEL" in up:
        return "additel_well"
    if "HART" in up or "FLUKE" in up:
        return "fluke_scpi"
    return "generic_scpi"
