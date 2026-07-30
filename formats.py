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


# Candidates tried by HeatSource.verify_commands.
#
# Two dialects live here. Fluke/Hart wells use standard SCPI SOURce naming
# (SOUR:SPO). Additel does NOT: in their published command sets the root
# keyword is the measured quantity, so a pressure controller uses
# "PRESsure:TARGet <value>" and "PRESsure:MODE", with no SOURce subsystem at
# all. The TEMPerature:* forms below are that same pattern applied to a
# temperature source -- inferred from Additel's house style, which is exactly
# why they are probed and confirmed rather than assumed.
SP_READ_CANDIDATES = (
    # Additel style
    "TEMPerature:TARGet?", "TEMP:TARG?",
    "SOURce:TEMPerature:TARGet?", "TEMPerature:SPOint?",
    # Fluke / standard SCPI style
    "SOUR:SPO?", "SOUR:SPOint?", "SOUR:TEMP:SPO?", "SOUR:SPO:TEMP?",
    "SETP?", "SP?",
    # Hart classic serial
    "s",
)

VALUE_CANDIDATES = (
    # Additel style: the bare quantity, or an explicit value/measure node
    "TEMPerature?", "TEMP?", "TEMPerature:VALue?", "TEMP:VAL?",
    "TEMPerature:MEASure?", "MEASure:TEMPerature?",
    "SOURce:TEMPerature?", "TEMPerature:CURRent?",
    # Additel's controllers expose a comprehensive status query whose first
    # field is the live value; the temperature analogue is worth trying.
    "TEMPerature:CONTrol:INFO?",
    # Fluke / standard SCPI style
    "SOUR:SENS:DAT? TEMP", "SOUR:SENS:DATA?", "MEAS:TEMP?", "MEAS?",
    "SOUR:TEMP?",
    # Hart classic serial
    "t",
)

UNIT_CANDIDATES = ("UNIT:TEMPerature?", "UNIT:TEMP?",
                   "TEMPerature:UNIT?", "u")

# Read-only queries used to find the heat/cool control command. Only the
# QUERY form is ever sent during discovery, so probing can never start the
# instrument heating; the paired writes are inferred from whichever answers.
CONTROL_PAIRS = {
    "TEMPerature:MODE?": ("TEMPerature:MODE 1", "TEMPerature:MODE 0"),
    "TEMP:MODE?": ("TEMP:MODE 1", "TEMP:MODE 0"),
    "TEMPerature:CONTrol?": ("TEMPerature:CONTrol 1",
                             "TEMPerature:CONTrol 0"),
    "OUTP:STAT?": ("OUTP:STAT 1", "OUTP:STAT 0"),
    "OUTPut:STATe?": ("OUTPut:STATe ON", "OUTPut:STATe OFF"),
}

# Optional extras worth knowing about if the instrument offers them.
STABLE_CANDIDATES = ("TEMPerature:STABle?", "TEMP:STAB?", "SOUR:STAB:COND?")

# A verified set-point read implies its paired write command.
WRITE_PAIRS = {
    "TEMPerature:TARGet?": "TEMPerature:TARGet {value}",
    "TEMP:TARG?": "TEMP:TARG {value}",
    "SOURce:TEMPerature:TARGet?": "SOURce:TEMPerature:TARGet {value}",
    "TEMPerature:SPOint?": "TEMPerature:SPOint {value}",
    "SOUR:SPO?": "SOUR:SPO {value}",
    "SOUR:SPOint?": "SOUR:SPOint {value}",
    "SOUR:TEMP:SPO?": "SOUR:TEMP:SPO {value}",
    "SOUR:SPO:TEMP?": "SOUR:SPO:TEMP {value}",
    "SETP?": "SETP {value}",
    "SP?": "SP {value}",
    "s": "s={value}",
}

# Sent to see whether the instrument keeps a usable error queue. Additel
# documents SYSTem:ERRor? as the way to check whether a control command was
# accepted, which makes discovery far more reliable than guessing from
# replies alone.
ERROR_QUERY = "SYSTem:ERRor?"
NONSENSE_COMMAND = "ZZQQ:NOSUCH?"

# SCPI unit IDs from Additel's published unit table.
UNIT_ID_NAMES = {"1000": "K", "1001": "°C", "1002": "°F", "1003": "°R",
                 "999": "°Re"}
UNIT_NAME_IDS = {"K": "1000", "C": "1001", "F": "1002", "R": "1003"}


def second_field(reply):
    """Second comma-separated field of a reply, or ''.

    Additel replies to a target query with value AND unit, e.g.
    "60.0000,1001" or "0.10000,MPa". That trailing field is what a write
    command has to echo back.
    """
    if not reply:
        return ""
    parts = [p.strip() for p in str(reply).split(",")]
    return parts[1] if len(parts) > 1 and parts[1] else ""


def plausible_unit_token(token):
    """True if a reply field really looks like a unit, not another number.

    Some instruments answer a query with several numbers (value, target,
    unit, ...). Without this check the second number would be stored as the
    unit and then sent back on every write.
    """
    token = (token or "").strip()
    if not token:
        return False
    if token in UNIT_ID_NAMES:
        return True
    try:
        float(token)
    except ValueError:
        return len(token) <= 12          # an alphabetic unit name like "C"
    return False                          # a bare number that is not a unit id


def unit_token_for(unit_name):
    """Best-guess unit token ('1001') for a display unit ('°C')."""
    key = (unit_name or "").strip().upper().replace("°", "").replace("DEG", "")
    return UNIT_NAME_IDS.get(key, "")


def describe_unit_token(token):
    """Human label for a unit token, for logs and dialogs."""
    token = (token or "").strip()
    if not token:
        return "(none)"
    if token in UNIT_ID_NAMES:
        return f"{token} = {UNIT_ID_NAMES[token]}"
    return token


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
