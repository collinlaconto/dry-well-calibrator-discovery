"""Visual design for the calibration suite.

A dark instrument console. The layout discipline comes from the rack
direction — hairlines, generous spacing, one idea per panel — while the
readouts borrow from a bench instrument's own display: large figures in a
warm amber, the reference in cyan, so the run you are watching is legible
from across the room.

Measured values are set in a monospace face with tabular figures so decimal
points line up down a column, the same reason instrument front panels use
fixed-width digits. Colour carries meaning and nothing else: cyan for the
reference, amber for a measured value or a wait, green for in tolerance, red
for out of tolerance.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------- palette --
INK = "#1f2937"        # primary text
DIM = "#64748b"        # secondary text
LINE = "#cfd8e5"       # borders and rules
PANEL = "#ffffff"      # cards and tables
SHELL = "#f4f7fb"      # workspace background
NAV = "#123a61"        # module rail
NAV_TEXT = "#d8e4ef"
NAV_RULE = "#315879"
NAV_ACTIVE = "#1d5688"
BLUE = "#1f6fb2"       # actions
CYAN = "#087f8c"       # the reference probe
GREEN = "#1f7a48"      # in tolerance, connected
AMBER = "#a25a00"      # measured value, waiting
RED = "#b42318"        # out of tolerance, stop
TRACK = "#edf2f7"      # inset wells behind bars
TOL_FILL = "#dceef4"   # tolerance envelope
TOL_EDGE = "#8bbdca"
HILITE = "#e8eef6"
SELECT = "#dcecff"

_MONO_CHOICES = ("Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono",
                 "Liberation Mono", "Courier New")
_UI_CHOICES = ("Segoe UI", "SF Pro Text", "Inter", "DejaVu Sans", "Helvetica")

FONTS = {}


def _pick(root, choices, fallback):
    try:
        available = set(tkfont.families(root))
    except Exception:
        return fallback
    for name in choices:
        if name in available:
            return name
    return fallback


def apply(root):
    """Theme a Tk root window. Returns the font dictionary."""
    ui = _pick(root, _UI_CHOICES, "TkDefaultFont")
    mono = _pick(root, _MONO_CHOICES, "TkFixedFont")

    FONTS.update({
        "ui": (ui, 9),
        "ui_bold": (ui, 9, "bold"),
        "small": (ui, 9),
        "label": (ui, 8),          # uppercase micro-labels
        "title": (ui, 16, "bold"),
        "heading": (ui, 11, "bold"),
        "mono": (mono, 10),
        "mono_small": (mono, 9),
        "readout": (mono, 20),     # the big measured number
        "readout_sm": (mono, 12),
    })

    try:
        root.configure(background=SHELL)
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")        # the most themable built-in theme
    except tk.TclError:
        pass

    style.configure(".", background=SHELL, foreground=INK,
                    font=FONTS["ui"], borderwidth=0)
    style.configure("TFrame", background=SHELL)
    style.configure("Card.TFrame", background=PANEL, relief="solid",
                    borderwidth=1, bordercolor=LINE)
    style.configure("Nav.TFrame", background=NAV)
    style.configure("TLabel", background=SHELL, foreground=INK)
    style.configure("Card.TLabel", background=PANEL, foreground=INK)
    style.configure("Dim.TLabel", background=SHELL, foreground=DIM,
                    font=FONTS["small"])
    style.configure("CardDim.TLabel", background=PANEL, foreground=DIM,
                    font=FONTS["small"])
    style.configure("Micro.TLabel", background=PANEL, foreground=DIM,
                    font=FONTS["label"])
    style.configure("Title.TLabel", background=SHELL, foreground=INK,
                    font=FONTS["title"])
    style.configure("Heading.TLabel", background=PANEL, foreground=INK,
                    font=FONTS["heading"])
    style.configure("Readout.TLabel", background=PANEL, foreground=INK,
                    font=FONTS["readout"])
    style.configure("ReadoutSm.TLabel", background=PANEL, foreground=DIM,
                    font=FONTS["readout_sm"])
    style.configure("Mono.TLabel", background=PANEL, foreground=INK,
                    font=FONTS["mono"])
    style.configure("Ok.TLabel", background=PANEL, foreground=GREEN,
                    font=FONTS["small"])
    style.configure("Warn.TLabel", background=PANEL, foreground=AMBER,
                    font=FONTS["small"])
    style.configure("Bad.TLabel", background=PANEL, foreground=RED,
                    font=FONTS["small"])
    style.configure("DeviceBadge.TLabel", background="#dceef4",
                    foreground="#075f6a", font=FONTS["ui_bold"],
                    padding=(7, 3))
    style.configure("DerivedBadge.TLabel", background="#e7edf6",
                    foreground="#334e73", font=FONTS["ui_bold"],
                    padding=(7, 3))
    style.configure("Pass.TLabel", background="#dff3e7", foreground=GREEN,
                    font=FONTS["ui_bold"], padding=(10, 5))
    style.configure("Fail.TLabel", background="#fde7e5", foreground=RED,
                    font=FONTS["ui_bold"], padding=(10, 5))
    style.configure("Invalid.TLabel", background="#fff1dc", foreground=AMBER,
                    font=FONTS["ui_bold"], padding=(10, 5))

    style.configure("TLabelframe", background=SHELL, bordercolor=LINE,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=SHELL, foreground=DIM,
                    font=FONTS["label"])

    style.configure("TButton", background=PANEL, foreground=INK,
                    bordercolor=LINE, relief="solid", borderwidth=1,
                    padding=(11, 6), font=FONTS["ui"])
    style.map("TButton",
              background=[("pressed", "#dbe5f0"), ("active", HILITE),
                          ("disabled", "#eef2f6")],
              foreground=[("disabled", "#94a3b8")],
              bordercolor=[("active", "#8aa4bf")])
    style.configure("Primary.TButton", background=BLUE, foreground="#ffffff",
                    bordercolor=BLUE, font=FONTS["ui_bold"])
    style.map("Primary.TButton",
              background=[("pressed", "#15598f"), ("active", "#2f80bd"),
                          ("disabled", "#b7c7d8")],
              foreground=[("disabled", "#eef3f8")],
              bordercolor=[("active", "#2f80bd")])
    style.configure("Danger.TButton", background="#fff5f4", foreground=RED,
                    bordercolor="#e7aaa5")
    style.map("Danger.TButton",
              background=[("active", "#fde7e5")],
              bordercolor=[("active", RED)])

    style.configure("TEntry", fieldbackground=TRACK, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, relief="solid",
                    borderwidth=1, padding=4, foreground=INK,
                    insertcolor=INK)
    style.map("TEntry", bordercolor=[("focus", CYAN)],
              fieldbackground=[("disabled", "#eef2f6")],
              foreground=[("disabled", "#94a3b8")])
    style.configure("TCombobox", fieldbackground=TRACK, background=HILITE,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                    arrowcolor=DIM, relief="solid", borderwidth=1, padding=3,
                    foreground=INK, selectbackground=TRACK,
                    selectforeground=INK)
    style.map("TCombobox", bordercolor=[("focus", CYAN)],
              fieldbackground=[("readonly", TRACK)],
              foreground=[("disabled", "#94a3b8")])
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT)
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*Listbox.background", PANEL)
    root.option_add("*Listbox.foreground", INK)
    root.option_add("*Listbox.selectBackground", SELECT)
    root.option_add("*Listbox.selectForeground", INK)
    root.option_add("*Listbox.highlightBackground", LINE)
    root.option_add("*Listbox.highlightColor", BLUE)
    root.option_add("*Listbox.borderWidth", 1)
    root.option_add("*Text.background", PANEL)
    root.option_add("*Text.foreground", INK)
    root.option_add("*Text.insertBackground", INK)
    style.configure("TCheckbutton", background=SHELL, foreground=INK,
                    indicatorcolor=TRACK, indicatorbackground=TRACK,
                    focuscolor=CYAN)
    style.map("TCheckbutton", background=[("active", SHELL)],
              indicatorcolor=[("selected", CYAN)],
              foreground=[("disabled", "#94a3b8")])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=INK, bordercolor=LINE, relief="solid",
                    borderwidth=1, rowheight=23, font=FONTS["mono_small"])
    style.configure("Treeview.Heading", background="#e9eef5",
                    foreground="#42566f", font=FONTS["ui_bold"],
                    relief="flat", padding=(6, 6))
    style.map("Treeview.Heading", background=[("active", HILITE)])
    style.map("Treeview", background=[("selected", SELECT)],
              foreground=[("selected", INK)])
    style.configure("TNotebook", background=SHELL, borderwidth=0)
    style.configure("TNotebook.Tab", background="#e9eef5", foreground=DIM,
                    padding=(12, 6), font=FONTS["ui"])
    style.map("TNotebook.Tab", background=[("selected", PANEL),
                                           ("active", HILITE)],
              foreground=[("selected", INK)])

    style.configure("TProgressbar", background=BLUE, troughcolor=TRACK,
                    bordercolor=TRACK, lightcolor=BLUE, darkcolor=BLUE)
    style.configure("Vertical.TScrollbar", background=HILITE,
                    troughcolor=SHELL, bordercolor=SHELL, arrowcolor=DIM)
    style.configure("Ref.TLabel", background=PANEL, foreground=CYAN,
                    font=FONTS["readout"])
    style.configure("Meas.TLabel", background=PANEL, foreground=AMBER,
                    font=FONTS["readout"])
    return FONTS


# ------------------------------------------------------------- widgets ----
RADIUS = 4


def round_rect(canvas, x1, y1, x2, y2, radius=RADIUS, **kw):
    """A rounded rectangle. Tk has no border radius, so draw one."""
    r = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


class Panel(tk.Frame):
    """A card with rounded corners.

    A canvas paints the rounded shape; content lives in an inner frame inset
    far enough that its square corners never reach the arcs.
    """

    def __init__(self, master, radius=RADIUS, pad=14, background=PANEL,
                 outside=SHELL, border=LINE, **kw):
        super().__init__(master, background=outside, bd=0,
                         highlightthickness=0, **kw)
        self.radius, self.fill, self.outline = radius, background, border
        self.bg = tk.Canvas(self, background=outside, highlightthickness=0,
                            bd=0)
        self.bg.place(x=0, y=0, relwidth=1, relheight=1)
        inset = max(pad, radius)
        self.inner = tk.Frame(self, background=background, bd=0,
                              highlightthickness=0)
        # ``place`` does not contribute to a parent's requested size.  These
        # cards contain live labels and variable DUT rows, so a placed inner
        # frame let MonitorCard/RunStrip collapse to a one-pixel-high shell.
        # Packing the content gives the outer card its natural, visible height;
        # the canvas remains placed behind it as the rounded background.
        self.inner.pack(fill="both", expand=True, padx=inset, pady=inset)
        self._shape = None
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self.bg.delete("all")
        self._shape = round_rect(self.bg, 0.5, 0.5, w - 0.5, h - 0.5,
                                 self.radius, fill=self.fill,
                                 outline=self.outline)

    def highlight(self, on):
        self.outline = CYAN if on else LINE
        self._redraw()


class Button(tk.Canvas):
    """A rounded button, since ttk buttons are square in every theme."""

    VARIANTS = {
        "": (PANEL, INK, LINE, HILITE),
        "Primary.TButton": (BLUE, "#ffffff", BLUE, "#2f80bd"),
        "Danger.TButton": ("#fff5f4", RED, "#e7aaa5", "#fde7e5"),
    }

    def __init__(self, master, text="", command=None, style="",
                 outside=SHELL, padx=13, pady=7, radius=8, **kw):
        fill, fg, edge, hover = self.VARIANTS.get(style, self.VARIANTS[""])
        self.fill, self.fg, self.edge, self.hover = fill, fg, edge, hover
        self.text, self.command, self.radius = text, command, radius
        self.padx, self.pady = padx, pady
        self._enabled = True
        self._state = "normal"
        self._focused = False
        font = FONTS.get("ui_bold" if style == "Primary.TButton" else "ui",
                         ("", 10))
        width, height = self._measure(master, text, font)
        super().__init__(master, width=width, height=height,
                         background=outside, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1, **kw)
        self._font = font
        self._draw()
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("normal"))
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Return>", self._keyboard_activate)
        self.bind("<space>", self._keyboard_activate)
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))

    def _measure(self, master, text, font):
        try:
            measured = tkfont.Font(root=master, font=font).measure(text)
        except Exception:
            measured = int(len(text) * 7.2)
        return measured + self.padx * 2, 16 + self.pady * 2

    def _set(self, state):
        if not self._enabled:
            return
        self._state = state
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(int(self.cget("width")), 10)
        h = max(int(self.cget("height")), 10)
        if not self._enabled:
            fill, fg, edge = "#eef2f6", "#94a3b8", "#d9e0e8"
        elif self._state == "hover":
            fill, fg, edge = self.hover, self.fg, self.edge
        elif self._state == "press":
            fill, fg, edge = self.edge, self.fg, self.edge
        else:
            fill, fg, edge = self.fill, self.fg, self.edge
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill,
                   outline=BLUE if self._focused else edge,
                   width=2 if self._focused else 1)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg,
                         font=self._font)

    def _press(self, _e=None):
        self._set("press")

    def _release(self, _e=None):
        if not self._enabled:
            return
        self._set("hover")
        if self.command:
            self.command()

    def _keyboard_activate(self, _event=None):
        if self._enabled and self.command:
            self.command()
        return "break"

    def _focus(self, focused):
        self._focused = focused
        self._draw()

    def configure(self, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            self.configure(cursor="hand2" if self._enabled else "arrow")
            self._state = "normal"
            self._draw()
        if "text" in kw:
            self.text = kw.pop("text")
            self._draw()
        if kw:
            super().configure(**kw)
    config = configure


class PageStack(ttk.Frame):
    """Sidebar navigation with swappable pages.

    Deliberately API-compatible with ttk.Notebook's add()/select() so the
    pages themselves did not have to be rewritten.
    """

    def __init__(self, master, title="Calibration Automation Suite",
                 subtitle=None,
                 groups=None, **kw):
        super().__init__(master, **kw)
        self.groups = groups or {}
        self.pages = []
        self.buttons = []
        self.tabs = []                    # labels, for tests and debugging
        self._current = None

        self.nav = tk.Frame(self, background=NAV, width=218)
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)

        brand = tk.Frame(self.nav, background=NAV)
        brand.pack(fill="x", padx=18, pady=(18, 16))
        tk.Label(brand, text=title, background=NAV, foreground="#ffffff",
                 font=FONTS.get("heading", ("", 11, "bold")),
                 anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(brand, text=subtitle.upper(), background=NAV,
                     foreground="#9fbbd2", font=FONTS.get("label", ("", 8)),
                     anchor="w").pack(fill="x")
        tk.Frame(self.nav, background=NAV_RULE, height=1).pack(fill="x")

        self.body = tk.Frame(self, background=SHELL)
        self.body.pack(side="left", fill="both", expand=True)

        self.status = tk.Label(self.nav, text="", background=NAV,
                               foreground="#b7cada", anchor="w",
                               justify="left", wraplength=182,
                               font=FONTS.get("small", ("", 9)))
        self.status.pack(side="bottom", fill="x", padx=18, pady=14)

    # -- Notebook-compatible surface --------------------------------------
    def add(self, child, text=""):
        label = self._clean(text)
        index = len(self.pages)
        group = self.groups.get(index)
        if group:
            tk.Label(self.nav, text=group.upper(), background=NAV,
                      foreground="#9fbbd2", anchor="w",
                     font=FONTS.get("label", ("", 8))
                     ).pack(fill="x", padx=16, pady=(14, 3))
        btn = tk.Label(self.nav, text=label, background=NAV,
                       foreground=NAV_TEXT, anchor="w", padx=16, pady=7,
                       font=FONTS.get("ui", ("", 10)), cursor="hand2")
        btn.pack(fill="x")
        btn.bind("<Button-1>", lambda e, i=index: self.select(i))
        btn.bind("<Enter>", lambda e, b=btn: self._hover(b, True))
        btn.bind("<Leave>", lambda e, b=btn: self._hover(b, False))
        self.pages.append(child)
        self.buttons.append(btn)
        self.tabs.append(label)
        child.place(in_=self.body, x=0, y=0, relwidth=1, relheight=1)
        if index == 0:
            self.select(0)
        else:
            child.lower()
        return child

    def select(self, index=None):
        if index is None:
            return self._current
        if isinstance(index, ttk.Frame):
            index = self.pages.index(index)
        if not (0 <= index < len(self.pages)):
            return self._current
        self.pages[index].lift()
        for i, btn in enumerate(self.buttons):
            on = (i == index)
            btn.configure(background=NAV_ACTIVE if on else NAV,
                          foreground="#ffffff" if on else NAV_TEXT,
                          font=FONTS.get("ui_bold" if on else "ui",
                                         ("", 10)))
        self._current = index
        return index

    def set_status(self, text):
        self.status.configure(text=text)

    def _hover(self, btn, entering):
        if btn.cget("background") == NAV_ACTIVE:
            return
        btn.configure(background="#194a76" if entering else NAV)

    @staticmethod
    def _clean(text):
        text = (text or "").strip()
        for sep in ("·", "-"):
            if sep in text:
                head, _, tail = text.partition(sep)
                if head.strip().isdigit():
                    text = tail.strip()
        return text


class Card(ttk.Frame):
    """A white panel with a hairline border."""

    def __init__(self, master, **kw):
        kw.setdefault("style", "Card.TFrame")
        kw.setdefault("padding", (14, 12))
        super().__init__(master, **kw)


class Chip(tk.Canvas):
    """Small status pill: a coloured dot and a label, fully rounded."""

    def __init__(self, master, text="", state="ok", outside=SHELL):
        super().__init__(master, height=24, background=outside,
                         highlightthickness=0, bd=0)
        self.outside = outside
        self._text, self._state = text, state
        self.bind("<Configure>", lambda e: self._draw())
        self.set(text, state)

    def set(self, text, state="ok"):
        self._text, self._state = text, state
        try:
            font = FONTS.get("small", ("", 9))
            width = tkfont.Font(root=self, font=font).measure(text) + 34
        except Exception:
            width = int(len(text) * 6.4) + 34
        self.configure(width=max(width, 60))
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(int(self.cget("width")), 20)
        h = max(int(self.cget("height")), 16)
        round_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=PANEL, outline=LINE)
        colour = {"ok": GREEN, "warn": AMBER, "bad": RED,
                  "idle": "#5d6884"}.get(self._state, DIM)
        cy = h / 2
        self.create_oval(11, cy - 3.5, 18, cy + 3.5, fill=colour, outline="")
        self.create_text(24, cy, anchor="w", text=self._text, fill=INK,
                         font=FONTS.get("small", ("", 9)))

class DeviationBand(tk.Canvas):
    """Error against the reference, drawn inside a tolerance envelope.

    The signature element: whether a probe is in tolerance is legible without
    reading a number, which is what an operator glancing at the bench needs.
    """

    def __init__(self, master, width=150, height=16, tolerance=0.05):
        super().__init__(master, width=width, height=height,
                         background=PANEL, highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.tolerance = tolerance or 0.05
        self._error = None
        self.bind("<Configure>", self._resize)
        self.redraw()

    def _resize(self, event):
        self.w, self.h = event.width, event.height
        self.redraw()

    def show(self, error, tolerance=None):
        if tolerance:
            self.tolerance = tolerance
        self._error = error
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = max(self.w, 20), max(self.h, 8)
        radius = h / 2
        round_rect(self, 0, 0, w, h, radius, fill=TRACK, outline="")
        # Envelope covers +/- tolerance across the middle 44% of the width,
        # so a reading twice the tolerance still lands on the scale.
        left, right = w * 0.28, w * 0.72
        round_rect(self, left, 1, right, h - 1, min(4, radius),
                   fill=TOL_FILL, outline=TOL_EDGE)
        self.create_line(w / 2, 2, w / 2, h - 2, fill="#4a5878")
        if self._error is None:
            return
        span = self.tolerance * 2.27          # tolerance sits at 22% from mid
        frac = max(-0.5, min(0.5, (self._error / span) if span else 0))
        x = w / 2 + frac * w
        inside = abs(self._error) <= self.tolerance
        r = 4.5
        self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r,
                         fill=GREEN if inside else RED, outline="")


class Readout(ttk.Frame):
    """A measured value with a micro-label above it."""

    def __init__(self, master, label="", value="—", style="Readout.TLabel"):
        super().__init__(master, style="Card.TFrame", padding=0)
        self.configure(style="Card.TFrame")
        ttk.Label(self, text=label.upper(), style="Micro.TLabel").pack(anchor="w")
        self.value = ttk.Label(self, text=value, style=style)
        self.value.pack(anchor="w")

    def set(self, text):
        self.value.configure(text=text)


class StabilityCurve(tk.Canvas):
    """The reference probe's recent history, with the stability band drawn.

    The question this answers is not "what is the temperature" but "has it
    stopped moving" — so the band is drawn around the running mean at the
    height the profile demands, and you can see the trace settle into it.
    """

    def __init__(self, master, width=420, height=96):
        super().__init__(master, width=width, height=height,
                         background=PANEL, highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.points = []            # [(timestamp, value)]
        self.band = None            # allowed peak-to-peak
        self.setpoint = None
        self.window = None          # seconds the band must hold
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        self.w, self.h = event.width, event.height
        self.redraw()

    def show(self, points, band=None, setpoint=None, window=None):
        self.points = list(points)
        self.band = band
        self.setpoint = setpoint
        self.window = window
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = max(self.w, 40), max(self.h, 30)
        pad_l, pad_r, pad_t, pad_b = 6, 54, 8, 16
        pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
        if pw <= 4 or ph <= 4:
            return
        round_rect(self, 0, 0, w, h, 8, fill=TRACK, outline="")

        values = [v for _, v in self.points if v is not None]
        if len(values) < 2:
            self.create_text(w / 2, h / 2, text="waiting for readings",
                             fill=DIM, font=FONTS.get("small", ("", 9)))
            return

        recent = values[-max(len(values) // 3, 8):]
        centre = sum(recent) / len(recent)
        half = max((self.band or 0.02) * 1.9, (max(values) - min(values)) * 0.62,
                   1e-4)
        lo, hi = centre - half, centre + half

        def y_of(v):
            return pad_t + ph - (v - lo) / (hi - lo) * ph

        # the band the reading must stay inside
        if self.band:
            top, bottom = y_of(centre + self.band / 2), y_of(centre - self.band / 2)
            self.create_rectangle(pad_l, top, pad_l + pw, bottom,
                                  fill=TOL_FILL, outline="")
            self.create_line(pad_l, top, pad_l + pw, top, fill=TOL_EDGE)
            self.create_line(pad_l, bottom, pad_l + pw, bottom, fill=TOL_EDGE)
            self.create_text(w - pad_r + 6, top, anchor="w",
                             text=f"+{self.band / 2:g}", fill=DIM,
                             font=FONTS.get("label", ("", 8)))
            self.create_text(w - pad_r + 6, bottom, anchor="w",
                             text=f"-{self.band / 2:g}", fill=DIM,
                             font=FONTS.get("label", ("", 8)))

        if self.setpoint is not None and lo <= self.setpoint <= hi:
            y = y_of(self.setpoint)
            self.create_line(pad_l, y, pad_l + pw, y, fill="#4a5878",
                             dash=(3, 3))

        t0 = self.points[0][0]
        span = max(self.points[-1][0] - t0, 1e-6)
        coords = []
        for t, v in self.points:
            if v is None:
                continue
            coords += [pad_l + (t - t0) / span * pw, y_of(v)]
        if len(coords) >= 4:
            self.create_line(*coords, fill=CYAN, width=2, smooth=False)
            self.create_oval(coords[-2] - 3, coords[-1] - 3,
                             coords[-2] + 3, coords[-1] + 3,
                             fill=CYAN, outline="")
        self.create_text(w - pad_r + 6, y_of(values[-1]), anchor="w",
                         text=f"{values[-1]:.3f}", fill=CYAN,
                         font=FONTS.get("mono_small", ("", 9)))
        secs = self.points[-1][0] - t0
        self.create_text(pad_l, h - 5, anchor="w",
                         text=f"last {secs:.0f}s", fill=DIM,
                         font=FONTS.get("label", ("", 8)))


class MonitorCard(Panel):
    """The run you are watching, at a size readable from across the bench."""

    def __init__(self, master):
        super().__init__(master, radius=RADIUS, pad=12)
        head = tk.Frame(self.inner, background=PANEL)
        head.pack(fill="x", padx=8, pady=(6, 0))
        left = tk.Frame(head, background=PANEL)
        left.pack(side="left")
        self.name = tk.Label(left, text="Nothing running", background=PANEL,
                             foreground=INK, anchor="w",
                             font=FONTS.get("heading", ("", 11, "bold")))
        self.name.pack(anchor="w")
        self.source = tk.Label(left, text="", background=PANEL, foreground=DIM,
                               anchor="w", font=FONTS.get("small", ("", 9)))
        self.source.pack(anchor="w")
        self.phase = tk.Label(head, text="", background=PANEL, foreground=AMBER,
                              font=FONTS.get("label", ("", 8)))
        self.phase.pack(side="right")

        body = tk.Frame(self.inner, background=PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(10, 6))

        nums = tk.Frame(body, background=PANEL)
        nums.pack(side="left", anchor="n")
        self.ref = self._readout(nums, "Reference", CYAN,
                                 FONTS.get("readout", ("", 20)))
        self.setp = self._readout(nums, "Set point", AMBER,
                                  FONTS.get("readout_sm", ("", 12)))
        self.progress = self._readout(nums, "Progress", INK,
                                      FONTS.get("readout_sm", ("", 12)))

        self.curve = StabilityCurve(body, width=380, height=104)
        self.curve.pack(side="left", fill="both", expand=True, padx=(22, 0))

        self.channels = tk.Frame(self.inner, background=PANEL)
        self.channels.pack(fill="x", padx=8, pady=(4, 6))
        self.rows = {}

    def _readout(self, parent, label, colour, valuefont):
        box = tk.Frame(parent, background=PANEL)
        box.pack(anchor="w", pady=(0, 8))
        tk.Label(box, text=label.upper(), background=PANEL, foreground=DIM,
                 anchor="w", font=FONTS.get("label", ("", 8))).pack(anchor="w")
        value = tk.Label(box, text="—", background=PANEL, foreground=colour,
                         anchor="w", font=valuefont)
        value.pack(anchor="w")
        return value

    def set_channels(self, channels, tolerance):
        for child in self.channels.winfo_children():
            child.destroy()
        self.rows = {}
        if not channels:
            return
        header = tk.Frame(self.channels, background=PANEL)
        header.pack(fill="x", pady=(0, 4))
        tk.Label(header, text="DEVICES UNDER TEST", background=PANEL,
                 foreground=DIM, font=FONTS.get("label", ("", 8))).pack(side="left")
        self.tol_label = tk.Label(header, text=f"TOLERANCE ±{tolerance:g}",
                                  background=PANEL, foreground=DIM,
                                  font=FONTS.get("label", ("", 8)))
        self.tol_label.pack(side="right")
        for channel in channels:
            row = tk.Frame(self.channels, background=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=channel, background=PANEL, foreground=DIM,
                     width=10, anchor="w",
                     font=FONTS.get("mono_small", ("", 9))).pack(side="left")
            verdict = tk.Label(row, text="", background=PANEL, foreground=DIM,
                               width=6, anchor="w",
                               font=FONTS.get("label", ("", 8)))
            verdict.pack(side="right")
            value = tk.Label(row, text="—", background=PANEL, foreground=INK,
                             width=9, anchor="e",
                             font=FONTS.get("mono", ("", 10)))
            value.pack(side="right", padx=(8, 8))
            band = DeviationBand(row, width=200, height=16,
                                 tolerance=tolerance)
            band.pack(side="left", fill="x", expand=True, padx=(8, 8))
            self.rows[channel] = (band, value, verdict)

    def update_channel(self, channel, error, tolerance):
        entry = self.rows.get(channel)
        if not entry:
            return
        band, value, verdict = entry
        band.show(error, tolerance)
        if error is None:
            value.configure(text="—", foreground=INK)
            verdict.configure(text="", foreground=DIM)
            return
        inside = abs(error) <= tolerance
        value.configure(text=f"{error:+.4f}", foreground=INK if inside else RED)
        verdict.configure(text="PASS" if inside else "FAIL",
                          foreground=GREEN if inside else RED)

    def update_head(self, name, source, phase, kind="wait"):
        self.name.configure(text=name)
        self.source.configure(text=source)
        colour = {"wait": AMBER, "run": CYAN, "done": GREEN, "bad": RED,
                  "idle": DIM}.get(kind, DIM)
        self.phase.configure(text=phase.upper(), foreground=colour)

    def update_values(self, reference, setpoint, progress):
        self.ref.configure(text=reference)
        self.setp.configure(text=setpoint)
        self.progress.configure(text=progress)


class RunStrip(Panel):
    """One running calibration, laid out like a unit in an instrument rack.

    Identity on the left, the live reference and progress in the middle, the
    deviation bands on the right. Four of these stack on one screen.
    """

    def __init__(self, master, on_select=None, run_id=None):
        super().__init__(master, radius=RADIUS, pad=10)
        self.run_id = run_id
        self.on_select = on_select
        self.selected = False
        self.bands = {}

        # left: which calibration, on what
        left = tk.Frame(self.inner, background=PANEL, width=228)
        left.pack(side="left", fill="y", padx=(6, 12), pady=6)
        self.name = tk.Label(left, text="", background=PANEL, foreground=INK,
                             anchor="w", justify="left", wraplength=210,
                             font=FONTS.get("ui_bold", ("", 10, "bold")))
        self.name.pack(fill="x")
        self.source = tk.Label(left, text="", background=PANEL, foreground=DIM,
                               anchor="w", justify="left", wraplength=210,
                               font=FONTS.get("small", ("", 9)))
        self.source.pack(fill="x")
        self.phase = tk.Label(left, text="", background=PANEL, foreground=AMBER,
                              anchor="w", font=FONTS.get("label", ("", 8)))
        self.phase.pack(fill="x", pady=(8, 0))

        tk.Frame(self.inner, background=LINE, width=1).pack(side="left",
                                                            fill="y", pady=6)

        # right: deviation from the reference
        right = tk.Frame(self.inner, background=PANEL, width=272)
        right.pack(side="right", fill="y", padx=(12, 6), pady=6)
        head = tk.Frame(right, background=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="DEVIATION", background=PANEL, foreground=DIM,
                 font=FONTS.get("label", ("", 8))).pack(side="left")
        self.tol_label = tk.Label(head, text="", background=PANEL,
                                  foreground=DIM,
                                  font=FONTS.get("label", ("", 8)))
        self.tol_label.pack(side="right")
        self.bandbox = tk.Frame(right, background=PANEL)
        self.bandbox.pack(fill="both", expand=True, pady=(6, 0))

        tk.Frame(self.inner, background=LINE, width=1).pack(side="right",
                                                            fill="y", pady=6)

        # middle: the live numbers and progress
        mid = tk.Frame(self.inner, background=PANEL)
        mid.pack(side="left", fill="both", expand=True, padx=16, pady=6)
        row = tk.Frame(mid, background=PANEL)
        row.pack(fill="x")
        self.ref = self._cell(row, "Reference", FONTS.get("readout", ("", 20)))
        self.setp = self._cell(row, "Set point", FONTS.get("readout_sm", ("", 12)))
        self.flat = self._cell(row, "Observed", FONTS.get("readout_sm", ("", 12)))
        self.track = tk.Canvas(mid, height=6, background=PANEL,
                               highlightthickness=0)
        self.track.pack(fill="x", pady=(11, 0))
        self._progress = 0.0
        self.track.bind("<Configure>", lambda e: self._draw_track())
        self.steps = tk.Label(mid, text="", background=PANEL, foreground=DIM,
                              anchor="w", font=FONTS.get("mono_small", ("", 9)))
        self.steps.pack(fill="x", pady=(6, 0))

        for w in (self, self.inner, left, mid, right, self.name, self.source,
                  self.phase, self.steps):
            w.bind("<Button-1>", self._clicked)
        self._bind_selection_tree()

    def _cell(self, parent, label, valuefont):
        box = tk.Frame(parent, background=PANEL)
        box.pack(side="left", padx=(0, 26))
        tk.Label(box, text=label.upper(), background=PANEL, foreground=DIM,
                 anchor="w", font=FONTS.get("label", ("", 8))).pack(anchor="w")
        value = tk.Label(box, text="—", background=PANEL, foreground=INK,
                         anchor="w", font=valuefont)
        value.pack(anchor="w")
        value.bind("<Button-1>", self._clicked)
        return value

    def _clicked(self, _event=None):
        if self.on_select:
            self.on_select(self.run_id)

    def _bind_selection_tree(self):
        """Make every visible part of the compact card select its run."""
        pending = [self]
        while pending:
            widget = pending.pop()
            widget.bind("<Button-1>", self._clicked)
            pending.extend(widget.winfo_children())

    def set_selected(self, on):
        self.selected = on
        self.highlight(on)

    def _draw_track(self):
        self.track.delete("all")
        w = max(self.track.winfo_width(), 2)
        round_rect(self.track, 0, 0, w, 6, 3, fill=TRACK, outline="")
        filled = w * max(0.0, min(1.0, self._progress))
        if filled > 3:
            round_rect(self.track, 0, 0, filled, 6, 3, fill=CYAN, outline="")

    # -- content ---------------------------------------------------------
    def update_head(self, name, source, phase, phase_kind="wait"):
        self.name.configure(text=name)
        self.source.configure(text=source)
        colour = {"wait": AMBER, "run": BLUE, "done": GREEN,
                  "bad": RED, "idle": DIM}.get(phase_kind, DIM)
        self.phase.configure(text=phase.upper(), foreground=colour)

    def update_values(self, reference, setpoint, flat, progress, steps):
        self.ref.configure(text=reference)
        self.setp.configure(text=setpoint)
        self.flat.configure(text=flat)
        self.steps.configure(text=steps)
        self._progress = progress
        self._draw_track()

    def set_channels(self, channels, tolerance):
        """Rebuild the deviation rows when a run's channel list changes."""
        for child in self.bandbox.winfo_children():
            child.destroy()
        self.bands = {}
        self.tol_label.configure(text=f"±{tolerance:g}")
        for channel in channels:
            row = tk.Frame(self.bandbox, background=PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=channel, background=PANEL, foreground=DIM,
                     width=9, anchor="w",
                     font=FONTS.get("mono_small", ("", 9))).pack(side="left")
            value = tk.Label(row, text="—", background=PANEL, foreground=INK,
                             width=7, anchor="e",
                             font=FONTS.get("mono_small", ("", 9)))
            value.pack(side="right")
            band = DeviationBand(row, width=120, height=15,
                                 tolerance=tolerance)
            band.pack(side="left", fill="x", expand=True, padx=(6, 6))
            self.bands[channel] = (band, value)
        self._bind_selection_tree()

    def update_channel(self, channel, error, tolerance=None):
        entry = self.bands.get(channel)
        if not entry:
            return
        band, value = entry
        band.show(error, tolerance)
        if error is None:
            value.configure(text="—", foreground=INK)
        else:
            inside = tolerance is None or abs(error) <= tolerance
            value.configure(text=f"{error:+.3f}",
                            foreground=INK if inside else RED)


def rule(master, background=PANEL):
    line = tk.Frame(master, background=LINE, height=1)
    return line
