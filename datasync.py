"""Logger comparison — synchronise data loggers against a reference probe.

Reads almost any logger export (CSV, TXT, XLSX) without per-brand
configuration: the timestamp and temperature columns are found by analysing
the data itself, and every file is aligned onto the reference probe's time
window before charting.

Grown from a standalone script into a page of the suite, so a calibration run
recorded here can be used directly as the reference instead of being exported
and re-imported.

Needs:  pip install pandas plotly openpyxl
"""

# -- Standard library ----------------------------------------------------------
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

# -- Third-party ---------------------------------------------------------------
# pandas and plotly are only needed by this tool. The rest of the suite runs
# without them, so a missing install disables this page rather than stopping
# the application from starting.
try:
    import pandas as pd
    HAS_PANDAS = True
    PANDAS_ERROR = ""
except ImportError as _exc:
    pd = None
    HAS_PANDAS = False
    PANDAS_ERROR = str(_exc)

# Plotly is only needed when actually building a chart; import lazily so the
# detection logic can be imported/tested without it installed.
try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False



# ==============================================================================
# SECTION 1 -- LOW-LEVEL FILE READING
# Helpers that read any CSV/TXT/XLSX into a clean pandas DataFrame, skipping
# whatever metadata preamble the brand happens to prepend.
# ==============================================================================

_ENCODINGS = ["utf-8-sig", "utf-8", "latin-1"]


def _detect_encoding(path: str) -> str:
    """Return the first encoding that can decode the file's first 8 KB."""
    sample = open(path, "rb").read(8192)
    for enc in _ENCODINGS:
        try:
            sample.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def _sniff_separator(path: str, enc: str, skiprows: int) -> str:
    """Guess the delimiter by comparing comma vs tab counts on a data line."""
    try:
        with open(path, encoding=enc, errors="replace") as fh:
            for _ in range(skiprows):
                fh.readline()
            line = fh.readline()
        return "\t" if line.count("\t") > line.count(",") else ","
    except OSError:
        return ","


def _score_header_line(fields: list) -> int:
    """
    Heuristic: how likely is this row to be the real column-header row?

    A header row tends to have several short, non-numeric, non-empty labels.
    Data rows have numbers; metadata rows have mostly empty cells. We reward
    rows with multiple distinct text labels and punish rows that are mostly
    numeric or mostly blank.
    """
    non_empty = [f.strip() for f in fields if f and f.strip()]
    if len(non_empty) < 2:
        return 0

    text_labels = 0
    numeric_cells = 0
    for f in non_empty:
        if re.fullmatch(r"[-+]?\d*\.?\d+([eE][-+]?\d+)?", f.strip()):
            numeric_cells += 1
        elif len(f) <= 40 and re.search(r"[A-Za-z]", f):
            text_labels += 1

    return text_labels * 2 - numeric_cells * 3


def _candidate_header_rows(path: str, enc: str, max_scan: int = 60):
    """
    Return a list of (row_index, header_score) for every plausible header row
    in the first `max_scan` lines, best score first. A caller then validates
    each candidate against the data beneath it and keeps the first that works.

    Returning ALL candidates (not just the single best-scoring row) is what
    lets us handle files with more than one header-like row -- e.g. the Elitech
    logger has a "Channel,Min,Max,..." summary block above the real
    "Sample,Temp (C),Date Time" header, and the Additel probe has metadata rows
    above "Step Time,REF1,...". A summary block may score well structurally, but
    only the real header is followed by parseable timestamp data.
    """
    sep = ","
    try:
        with open(path, encoding=enc, errors="replace") as fh:
            lines = [next(fh, None) for _ in range(max_scan)]
    except OSError:
        return []

    for line in lines:
        if line and line.count("\t") > line.count(","):
            sep = "\t"
            break

    scored = []
    for i, line in enumerate(lines):
        if line is None:
            break
        fields = line.rstrip("\n").split(sep)
        score = _score_header_line(fields)
        if score > 0:
            scored.append((i, score))

    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored


def _read_csv_at(path: str, enc: str, header_row: int):
    """Read a CSV/TXT using a specific header row index; return a clean DataFrame."""
    sep = _sniff_separator(path, enc, header_row)
    df = pd.read_csv(path, skiprows=header_row, sep=sep, encoding=enc,
                     dtype=str, engine="python")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, [c for c in df.columns
                    if c != "" and not str(c).startswith("Unnamed")]]
    return df


def read_table(path: str):
    """
    Read any supported file into a DataFrame of strings, auto-skipping the
    metadata preamble. Returns (DataFrame, sheet_name_or_None).

    For CSV/TXT, every candidate header row is evaluated and the one whose
    detected timestamp column parses MOST consistently is chosen. Scoring by
    parse-quality (not just "is a timestamp detectable") rejects metadata or
    summary blocks that accidentally line up a few parseable values: e.g. the
    Elitech summary block "Channel,Min,Max,..." sits above the real
    "Sample,Temp (C),Date Time" header, and only the real header yields a column
    that parses cleanly on every row.
    """
    if path.lower().endswith(".xlsx"):
        return _read_xlsx_table(path)

    enc = _detect_encoding(path)
    candidates = _candidate_header_rows(path, enc)
    if not candidates:
        candidates = [(0, 0)]

    fallback_df = None
    best = None  # (quality, -header_row, df)

    for header_row, _score in candidates:
        try:
            df = _read_csv_at(path, enc, header_row)
        except Exception:
            continue
        if df.empty or len(df.columns) < 2:
            continue
        if fallback_df is None:
            fallback_df = df

        tinfo = detect_time_column(df)
        if tinfo is None:
            continue

        # Measure how well the timestamp column actually parses
        quality = _time_quality(df, tinfo)
        # Prefer higher quality; on ties prefer the LATER header row (deeper in
        # the file = past the summary/metadata blocks)
        key = (quality, header_row)
        if best is None or key > best[0]:
            best = (key, df, tinfo)

    if best is not None:
        return best[1], None
    return (fallback_df if fallback_df is not None else pd.DataFrame()), None


def _time_quality(df: "pd.DataFrame", tinfo: dict) -> float:
    """
    Return a 0..1 quality score for a detected timestamp, used to choose
    between competing header-row interpretations.

    For absolute/split timestamps it's the fraction of rows that parsed.
    For elapsed time it's the fraction of rows matching the MM:SS pattern.
    A real data header scores near 1.0; a summary block that only accidentally
    lines up a few values scores lower.
    """
    if tinfo["kind"] in ("absolute", "split"):
        parsed = tinfo.get("parsed")
        return float(parsed.notna().mean()) if parsed is not None else 0.0
    if tinfo["kind"] == "elapsed":
        s = df[tinfo["col"]].dropna().astype(str).str.strip()
        if len(s) == 0:
            return 0.0
        return float(s.str.match(r"^\d{1,3}:\d{2}(:\d{2})?(\.\d+)?$").mean())
    return 0.0


def _read_xlsx_table(path: str):
    """
    Read an .xlsx file, choosing the sheet and header row automatically.

    For each sheet, every plausible header row is tried (best score first) and
    the first whose data has a detectable timestamp column is accepted. The
    sheet with the most data rows under a valid header wins -- this picks the
    real data sheet over metadata/events sheets.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)

    best = None  # (data_rows, sheet_title, df)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        # Rank candidate header rows by structural score
        scan = min(len(rows), 60)
        scored = []
        for i in range(scan):
            fields = [str(c) if c is not None else "" for c in rows[i]]
            s = _score_header_line(fields)
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda t: (-t[1], t[0]))
        if not scored:
            scored = [(0, 0)]

        for header_idx, _s in scored:
            headers = [str(c).strip() if c is not None else ""
                       for c in rows[header_idx]]
            records = rows[header_idx + 1:]
            df = pd.DataFrame(records, columns=headers)
            df = df.loc[:, [c for c in df.columns if c != ""]]
            if df.empty or len(df.columns) < 2:
                continue
            if detect_time_column(df) is not None:
                data_rows = len(df)
                if best is None or data_rows > best[0]:
                    best = (data_rows, ws.title, df)
                break  # accepted this sheet's header; move to next sheet

    wb.close()
    if best is None:
        return pd.DataFrame(), None
    return best[2], best[1]


# ==============================================================================
# SECTION 2 -- COLUMN AUTO-DETECTION
# Identify which column is the timestamp and which is the temperature, using
# both the column NAME and the actual VALUES.
# ==============================================================================

_NEGATIVE_VALUE_WORDS = [
    "humid", "rh", "dew", "pressure", "mbar", "ohm", "resist",
    "avg", "average", "std", "dev", "min", "max", "kinetic", "battery",
    "volt", "sample", "reading", "index", "count", "host", "alarm",
    "serial", "channel #",
]
_POSITIVE_VALUE_WORDS = [
    "temp", "temperature", "celsius", "degc", "deg c", "ref1", "ref2",
    "fahrenheit", "degf",
]
_TIME_WORDS = ["date", "time", "timestamp", "datetime"]


def _series_numeric_fraction(series: "pd.Series") -> float:
    """Fraction of non-null values that parse as finite numbers."""
    s = series.dropna().astype(str).str.strip()
    if len(s) == 0:
        return 0.0
    nums = pd.to_numeric(s, errors="coerce")
    return float(nums.notna().mean())


def _series_temp_plausibility(series: "pd.Series") -> float:
    """
    Fraction of numeric values that fall in a plausible temperature range.
    Generous band (-100..400 C) just to reject pressure (~1012) or humidity
    that happen to be numeric but sit outside a sane deg-C window.
    """
    nums = pd.to_numeric(series.dropna().astype(str).str.strip(), errors="coerce")
    nums = nums.dropna()
    if len(nums) == 0:
        return 0.0
    return float(((nums >= -100) & (nums <= 400)).mean())


def _name_score(col: str, positives: list, negatives: list) -> int:
    """Score a column name: +2 per positive word, -3 per negative word."""
    c = col.lower()
    score = 0
    for w in positives:
        if w in c:
            score += 2
    for w in negatives:
        if w in c:
            score -= 3
    # Degree-C / degree-F symbols are strong positive signals
    if "\u00b0c" in c or "\u00b0 c" in c:
        score += 2
    if "\u00b0f" in c:
        score += 2
    # A lone percent sign suggests humidity/RH -> negative
    if "%" in c:
        score -= 3
    # Ohms symbol suggests a resistance column -> negative
    if "\u03a9" in c:
        score -= 3
    return score


def detect_temperature_column(df: "pd.DataFrame"):
    """
    Pick the column most likely to hold the temperature reading.

    Combines name score, numeric fraction, and value plausibility. Returns the
    column name, or None if nothing scores positively.
    """
    ranked = rank_temperature_columns(df)
    return ranked[0][0] if ranked else None


def rank_temperature_columns(df: "pd.DataFrame"):
    """
    Return a list of (column_name, score) for every column that could plausibly
    be a temperature reading, best first.

    This powers the GUI's value-column dropdown: the top entry is the auto
    guess, and the rest are offered as overrides ordered by likelihood. A
    column qualifies if it is mostly numeric; it is then scored by how
    temperature-like its name is and how many values fall in a sane deg-C band.
    """
    scored = []
    for col in df.columns:
        num = _series_numeric_fraction(df[col])
        if num < 0.6:
            continue  # text columns can't be readings
        name = _name_score(col, _POSITIVE_VALUE_WORDS, _NEGATIVE_VALUE_WORDS)
        plaus = _series_temp_plausibility(df[col])
        score = name * 3 + plaus * 4 + num
        scored.append((col, score))
    scored.sort(key=lambda t: -t[1])
    # Keep only columns with a non-negative score so we don't offer, say, a
    # humidity column as a temperature option unless nothing better exists.
    positive = [(c, s) for c, s in scored if s > 0]
    return positive if positive else scored


def _looks_like_elapsed(series: "pd.Series") -> bool:
    """True if values look like MM:SS or HH:MM:SS elapsed time."""
    s = series.dropna().astype(str).str.strip().head(20)
    if len(s) == 0:
        return False
    hits = s.str.match(r"^\d{1,3}:\d{2}(:\d{2})?(\.\d+)?$")
    return float(hits.mean()) > 0.7


def _parse_datetime_series(series: "pd.Series", _cache={}):
    """
    Try to parse a column as datetimes. Returns a parsed datetime Series (NaT
    for failures) or None if the column clearly isn't datetimes.

    Performance: a small sample is parsed FIRST. If the sample fails to parse,
    the column is rejected immediately without running pandas' expensive
    dateutil fallback over every one of (potentially) tens of thousands of
    rows. Only columns that look promising on the sample are parsed in full.
    Results are cached by Series identity so repeated calls are free.
    """
    import warnings

    raw = series.dropna()
    if len(raw) == 0:
        return None

    cache_key = (id(series), len(series))
    if cache_key in _cache:
        return _cache[cache_key]

    def _store(val):
        if len(_cache) > 256:
            _cache.clear()
        _cache[cache_key] = val
        return val

    # Native datetime objects (Excel) -- cheap, handle directly
    if pd.api.types.is_datetime64_any_dtype(series):
        return _store(pd.to_datetime(series, errors="coerce"))
    if raw.map(lambda v: isinstance(v, datetime)).mean() > 0.5:
        return _store(pd.to_datetime(series, errors="coerce"))

    s = series.astype(str).str.strip()

    # -- FAST PRE-CHECK on a sample --------------------------------------------
    # Reject obvious non-dates (elapsed "58:35.0", plain numbers, short codes)
    # before paying for a full-column dateutil parse. We sample up to 25 values
    # spread across the column and require most of them to parse.
    sample = s.dropna()
    if len(sample) > 25:
        step = max(1, len(sample) // 25)
        sample = sample.iloc[::step].head(25)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sample_parsed = pd.to_datetime(sample, errors="coerce")
    if float(sample_parsed.notna().mean()) < 0.7:
        return _store(None)   # sample failed -> not a datetime column

    # -- Full parse (only reached for promising columns) -----------------------
    best, best_ok = None, 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for kwargs in ({}, {"dayfirst": True}):
            parsed = pd.to_datetime(s, errors="coerce", **kwargs)
            ok = float(parsed.notna().mean())
            if ok > best_ok:
                best_ok, best = ok, parsed
            if best_ok >= 0.99:
                break
    return _store(best if best_ok >= 0.7 else None)


def _has_no_date_part(series) -> bool:
    """True if the raw text carries a time but no date.

    "00:01" parses happily as a datetime -- pandas quietly supplies today's
    date. For an elapsed-time export like the ADT286's "Step Time" that is
    badly wrong: it anchors the run to whatever day the file was opened and
    never asks for the real start time. Detecting that the date was invented
    lets elapsed handling take over.
    """
    raw = series.dropna().astype(str).str.strip().head(30)
    if len(raw) == 0:
        return False
    dated = raw.str.contains(r"[/\-]|[A-Za-z]{3}", regex=True)
    return not bool(dated.any())


def _is_date_only(parsed) -> bool:
    """True if every parsed value falls exactly on midnight.

    Such a column is a date, not a timestamp: using it would collapse a whole
    day of readings onto one instant.
    """
    values = parsed.dropna()
    if len(values) < 2:
        return False
    try:
        midnight = ((values.dt.hour == 0) & (values.dt.minute == 0)
                    & (values.dt.second == 0))
    except AttributeError:
        return False
    return bool(midnight.all())


def detect_time_column(df: "pd.DataFrame"):
    """
    Identify the timestamp column and how to interpret it. Returns a dict:
      {"kind": "absolute", "col": name, "parsed": Series}
      {"kind": "split", "date_col": c1, "time_col": c2, "parsed": Series}
      {"kind": "elapsed", "col": name}
    or None if nothing usable is found.
    """
    cols = list(df.columns)

    def name_has_time_word(c):
        cl = c.lower()
        return any(w in cl for w in _TIME_WORDS)

    timeish = [c for c in cols if name_has_time_word(c)]
    others = [c for c in cols if c not in timeish]

    # 1. Split Date + Time columns.
    #
    # Checked BEFORE a single column, because a lone "Date" column parses
    # perfectly well as datetimes -- at midnight. Accepting it would silently
    # throw away the time of day and stack every row of the day on one instant,
    # which destroys the alignment this whole tool exists to do. So whenever a
    # date column has a companion time column, the pair wins.
    date_like = [c for c in cols if "date" in c.lower() and "time" not in c.lower()]
    time_like = [c for c in cols if c.lower() == "time"
                 or ("time" in c.lower() and "date" not in c.lower())]
    for dcol in date_like:
        for tcol in time_like:
            combined = (df[dcol].astype(str).str.strip() + " "
                        + df[tcol].astype(str).str.strip())
            parsed = _parse_datetime_series(combined)
            if parsed is not None and parsed.notna().mean() >= 0.7:
                return {"kind": "split", "date_col": dcol, "time_col": tcol,
                        "parsed": parsed}

    # 2. Single absolute-datetime column. A column carrying only midnights is
    # a date without a time; keep looking and use it only as a last resort.
    date_only = None
    clock_only = None
    for col in timeish + others:
        if _looks_like_elapsed(df[col]) and _has_no_date_part(df[col]):
            continue        # elapsed time; handled below, not as a timestamp
        parsed = _parse_datetime_series(df[col])
        if parsed is not None and parsed.notna().mean() >= 0.7:
            if _is_date_only(parsed):
                if date_only is None:
                    date_only = {"kind": "absolute", "col": col,
                                 "parsed": parsed, "date_only": True}
                continue
            if _has_no_date_part(df[col]):
                # A time of day with no date. Usable, but only if nothing
                # better turns up, since the date was invented.
                if clock_only is None:
                    clock_only = {"kind": "absolute", "col": col,
                                  "parsed": parsed, "clock_only": True}
                continue
            return {"kind": "absolute", "col": col, "parsed": parsed}

    # 3. Elapsed MM:SS column
    for col in timeish + others:
        if _looks_like_elapsed(df[col]):
            return {"kind": "elapsed", "col": col}

    # 4. Fall back to the weaker interpretations, best first.
    return clock_only or date_only


# ==============================================================================
# SECTION 3 -- HIGH-LEVEL LOADERS
# ==============================================================================

def _stitch_elapsed_to_abs(elapsed_str: "pd.Series", start: datetime) -> "pd.Series":
    """Convert an elapsed MM:SS / HH:MM:SS column to absolute datetimes."""
    def to_seconds(v):
        v = str(v).strip()
        m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", v)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        m = re.fullmatch(r"(\d+):(\d{2}(?:\.\d+)?)", v)
        if m:
            return int(m[1]) * 60 + float(m[2])
        return float(v)

    secs = elapsed_str.apply(to_seconds)
    offsets = (secs.diff() < 0).cumsum() * 3600
    total = secs + offsets
    return total.apply(lambda s: start + timedelta(seconds=float(s)))


def analyze_file(path: str):
    """
    Read a file once and return everything the GUI needs to show dropdowns:

        {
          "df":            the cleaned DataFrame,
          "sheet":         sheet name (xlsx) or None,
          "columns":       all column names,
          "time_info":     dict from detect_time_column (the auto guess) or None,
          "time_guess":    display string for the guessed timestamp column,
          "value_guess":   guessed temperature column name or None,
          "value_options": ranked list of plausible temperature column names,
          "needs_start":   True if the timestamp is elapsed (needs a start time),
        }

    This does all detection in a SINGLE read so the GUI doesn't re-open the file
    for every dropdown. run_pipeline can then be told exactly which columns the
    user selected, skipping detection entirely on the real run.
    """
    df, sheet = read_table(path)
    if df.empty:
        return {"df": df, "sheet": sheet, "columns": [], "time_info": None,
                "time_guess": None, "value_guess": None, "value_options": [],
                "needs_start": False}

    tinfo = detect_time_column(df)
    if tinfo is None:
        time_guess = None
        needs_start = False
    elif tinfo["kind"] == "absolute":
        time_guess = tinfo["col"]
        needs_start = False
    elif tinfo["kind"] == "split":
        time_guess = f"{tinfo['date_col']} + {tinfo['time_col']}"
        needs_start = False
    else:  # elapsed
        time_guess = tinfo["col"]
        needs_start = True

    ranked = rank_temperature_columns(df)
    value_options = [c for c, _ in ranked]
    value_guess = value_options[0] if value_options else None

    return {
        "df": df, "sheet": sheet, "columns": list(df.columns),
        "time_info": tinfo, "time_guess": time_guess,
        "value_guess": value_guess, "value_options": value_options,
        "needs_start": needs_start,
    }


def build_series(df: "pd.DataFrame", time_info: dict, value_col: str,
                 start: datetime = None, time_col_override: str = None):
    """
    Turn an already-read DataFrame into the clean two-column result
    (_abs_time, _value) using a known timestamp interpretation and a chosen
    value column. Separated from file reading so the GUI -- which already read
    the file in analyze_file -- doesn't have to read it again.

    time_col_override : if the user picked a different timestamp column than the
                        auto guess, pass its name here; it is re-analysed as an
                        absolute or elapsed column.
    """
    # Honour a user override of the timestamp column
    if time_col_override and (time_info is None
                              or time_col_override != time_info.get("col")):
        parsed = _parse_datetime_series(df[time_col_override])
        if parsed is not None:
            time_info = {"kind": "absolute", "col": time_col_override,
                         "parsed": parsed}
        elif _looks_like_elapsed(df[time_col_override]):
            time_info = {"kind": "elapsed", "col": time_col_override}
        else:
            raise ValueError(
                f"Selected time column '{time_col_override}' does not parse "
                f"as dates or elapsed time."
            )

    if time_info is None:
        raise ValueError("No timestamp column available.")

    if time_info["kind"] in ("absolute", "split"):
        abs_time = time_info["parsed"]
    else:  # elapsed
        if start is None:
            raise ValueError(
                "START_TIME_REQUIRED: this file uses elapsed time "
                f"('{time_info['col']}') and needs a start date/time."
            )
        abs_time = _stitch_elapsed_to_abs(df[time_info["col"]], start)

    values = pd.to_numeric(df[value_col].astype(str).str.strip(), errors="coerce")
    out = pd.DataFrame({"_abs_time": abs_time, "_value": values}).dropna()
    return out.sort_values("_abs_time").reset_index(drop=True)


def load_any(path: str, start: datetime = None, value_col: str = None,
             time_col: str = None, log_fn=print) -> "pd.DataFrame":
    """
    Universal loader. Reads any file, auto-detects timestamp and temperature
    columns (unless overridden), returns a DataFrame: _abs_time, _value.

    value_col : force a specific temperature column (optional override)
    time_col  : force a specific timestamp column (optional override)
    """
    info = analyze_file(path)
    df = info["df"]
    if df.empty:
        raise ValueError(f"{os.path.basename(path)}: no tabular data found.")

    tinfo = info["time_info"]
    if tinfo is None and not time_col:
        raise ValueError(
            f"{os.path.basename(path)}: could not find a timestamp column.\n"
            f"  Columns seen: {list(df.columns)}"
        )

    vcol = value_col if (value_col and value_col in df.columns) else info["value_guess"]
    if vcol is None:
        raise ValueError(
            f"{os.path.basename(path)}: could not find a temperature column.\n"
            f"  Columns seen: {list(df.columns)}"
        )

    out = build_series(df, tinfo, vcol, start=start, time_col_override=time_col)

    # Build a description for the log
    if time_col:
        tdesc = f"'{time_col}' (override)"
    elif tinfo and tinfo["kind"] == "split":
        tdesc = f"'{tinfo['date_col']}' + '{tinfo['time_col']}' (split)"
    elif tinfo and tinfo["kind"] == "elapsed":
        tdesc = f"'{tinfo['col']}' (elapsed, anchored to {start})"
    elif tinfo:
        tdesc = f"'{tinfo['col']}' (absolute)"
    else:
        tdesc = "(none)"
    src = f" [sheet: {info['sheet']}]" if info["sheet"] else ""
    log_fn(f"  time {tdesc}; value '{vcol}'{src}")
    return out


# ==============================================================================
# SECTION 4 -- ALIGNMENT
# ==============================================================================

def clip_to_window(df: "pd.DataFrame", name: str,
                   start: datetime, end: datetime, log_fn=print) -> "pd.DataFrame":
    """Trim rows outside [start, end]; report what was removed."""
    before = len(df)
    mask = (df["_abs_time"] >= start) & (df["_abs_time"] <= end)
    out = df[mask].copy()
    dropped = before - len(out)
    if dropped:
        early = max(0.0, (start - df["_abs_time"].min()).total_seconds())
        late = max(0.0, (df["_abs_time"].max() - end).total_seconds())
        bits = []
        if early:
            bits.append(f"{early:.0f}s before start")
        if late:
            bits.append(f"{late:.0f}s after end")
        log_fn(f"  trimmed {dropped} row(s) from '{name}' ({', '.join(bits)})")
    else:
        log_fn(f"  '{name}' fully within window")
    return out


# ==============================================================================
# SECTION 5 -- CHART
# ==============================================================================

def build_chart(probe_df, device_dfs, device_names, output_path):
    """Write a self-contained interactive HTML chart."""
    if not HAS_PLOTLY:
        raise RuntimeError("plotly is not installed. Run: pip install plotly")

    fig = go.Figure()
    if probe_df is not None and not probe_df.empty:
        fig.add_trace(go.Scatter(
            x=probe_df["_abs_time"], y=probe_df["_value"],
            mode="lines", name="Probe",
            line=dict(width=1.5, color="#00b4d8"),
        ))

    palette = ["#ef233c", "#f77f00", "#2dc653", "#9b5de5", "#f15bb5",
               "#fee440", "#00bbf9", "#fb5607", "#e63946", "#2a9d8f",
               "#e9c46a", "#264653"]
    for i, (df, name) in enumerate(zip(device_dfs, device_names)):
        if df.empty:
            continue
        fig.add_trace(go.Scatter(
            x=df["_abs_time"], y=df["_value"],
            mode="lines+markers", name=name,
            line=dict(width=2, color=palette[i % len(palette)]),
            marker=dict(size=4),
        ))

    fig.update_layout(
        title="Temperature -- Probe + Data Loggers (auto-synced)",
        xaxis_title="Time", yaxis_title="Temperature (\u00b0C)",
        template="plotly_dark", hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        font=dict(family="monospace", size=12),
    )
    fig.write_html(output_path)


# ==============================================================================
# SECTION 6 -- PIPELINE
# ==============================================================================

def run_pipeline(probe_path, start_dt, device_paths, output_path,
                 probe_value_col=None, probe_time_col=None,
                 device_value_cols=None, device_time_cols=None,
                 log_fn=print) -> bool:
    """
    Load probe + devices, align, write chart.

    Column selection is automatic unless overrides are supplied:
      probe_value_col / probe_time_col : column names for the probe
      device_value_cols / device_time_cols : dicts keyed by device path,
            giving the chosen value/time column for each file (from the GUI
            dropdowns). Missing entries fall back to auto-detection.
    """
    device_value_cols = device_value_cols or {}
    device_time_cols = device_time_cols or {}
    try:
        probe_df = None
        if probe_path:
            log_fn(f"Loading probe: {os.path.basename(probe_path)}")
            probe_df = load_any(probe_path, start=start_dt,
                                value_col=probe_value_col,
                                time_col=probe_time_col, log_fn=log_fn)
            log_fn(f"  {len(probe_df):,} rows | "
                   f"{probe_df['_abs_time'].min():%Y-%m-%d %H:%M:%S} -> "
                   f"{probe_df['_abs_time'].max():%Y-%m-%d %H:%M:%S}")

        device_dfs, device_names = [], []
        for p in device_paths:
            name = os.path.splitext(os.path.basename(p))[0]
            log_fn(f"Loading device: {os.path.basename(p)}")
            df = load_any(p, start=start_dt,
                          value_col=device_value_cols.get(p),
                          time_col=device_time_cols.get(p), log_fn=log_fn)
            log_fn(f"  {len(df):,} rows | "
                   f"{df['_abs_time'].min():%Y-%m-%d %H:%M:%S} -> "
                   f"{df['_abs_time'].max():%Y-%m-%d %H:%M:%S}")
            device_dfs.append(df)
            device_names.append(name)

        if probe_df is not None and not probe_df.empty:
            start = probe_df["_abs_time"].min()
            end = probe_df["_abs_time"].max()
            log_fn(f"\nAligning to probe window "
                   f"{start:%Y-%m-%d %H:%M:%S} -> {end:%Y-%m-%d %H:%M:%S}")
            device_dfs = [clip_to_window(d, n, start, end, log_fn)
                          for d, n in zip(device_dfs, device_names)]
            for d, n in zip(device_dfs, device_names):
                if d.empty:
                    log_fn(f"  WARNING: '{n}' has no overlap with the probe window.")

        log_fn("\nBuilding chart...")
        build_chart(probe_df, device_dfs, device_names, output_path)
        log_fn(f"Done. Chart saved to: {output_path}")
        return True

    except Exception as exc:
        import traceback
        log_fn(f"\nError: {exc}")
        log_fn(traceback.format_exc())
        return False




# ==============================================================================
# SECTION 7 -- BRIDGE TO CALIBRATION RUNS
# A run recorded in this application already holds exactly what the reference
# probe file provides: timestamped readings from the reference channel. Using
# it directly removes an export-and-reimport round trip, and guarantees the
# comparison is against the same data the certificate was built from.
# ==============================================================================

def series_from_run(engine, channel=None):
    """Build a probe series from a finished (or running) calibration.

    Returns the same two-column frame as load_any: _abs_time and _value.
    With no channel given, the run's reference probe is used.
    """
    if not HAS_PANDAS:
        raise RuntimeError("pandas is not installed. Run: pip install pandas")
    channel = channel or engine.profile.get("reference_channel")
    times, values = [], []
    for result in engine.results:
        for sample in result.samples:
            value = (sample.get("ref") if channel ==
                     engine.profile.get("reference_channel")
                     else sample.get("duts", {}).get(channel))
            if value is None:
                continue
            times.append(datetime.fromtimestamp(sample["t"]))
            values.append(float(value))
    frame = pd.DataFrame({"_abs_time": times, "_value": values})
    return frame.sort_values("_abs_time").reset_index(drop=True)


def run_channels(engine):
    """Channels available from a run: the reference first, then each device."""
    reference = engine.profile.get("reference_channel")
    return ([reference] if reference else []) + list(
        engine.profile.get("dut_channels", []))


def sample_count(engine):
    return sum(len(result.samples) for result in engine.results)
