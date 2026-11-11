"""The printer models this program drives, and the paper each one prints on.

Layouts are written in CTP500 dots (1/8 mm, 384 across). A printer with more dots across draws everything
scaled up by `Paper.scale`, so a print comes out the same size on either printer, just sharper on the finer one.
"""

from __future__ import annotations

from dataclasses import dataclass

LAYOUT_WIDTH = 384          # the width layouts are written for (the CTP500's 48 mm at 203 dpi)
FINE_SCALE = 1.25           # Paper.scale from which dots count as finer than the CTP500's (M02 Pro: 1.4 / 1.5)


@dataclass(frozen=True)
class Paper:
    width: int = LAYOUT_WIDTH   # dots a print uses across
    dpi: int = 203
    head: int = 0               # dots across the print head when a print uses fewer (0: the same as width)
    left: int = 0               # head dots left of the print

    @property
    def scale(self) -> float:
        return self.width / LAYOUT_WIDTH

    @property
    def dots_per_mm(self) -> float:
        return self.dpi / 25.4

    @property
    def fine(self) -> bool:
        """Dots finer than the CTP500's (the M02 Pro): photos are drawn differently there (styles.thermal_photo)."""
        return self.scale >= FINE_SCALE

    @property
    def head_width(self) -> int:
        return self.head or self.width

    @property
    def row_bytes(self) -> int:
        """Bytes in each row sent to the printer: the whole head."""
        return (self.head_width + 7) // 8

    def mm(self, dots: int) -> int:
        return round(dots * 25.4 / self.dpi)


@dataclass(frozen=True)
class Model:
    key: str
    label: str                      # the full name (setup messages)
    short: str                      # what the app and alerts call it
    paper: Paper
    dialects: tuple[str, ...]       # the first is the default
    chunk_size: int                 # Bluetooth pacing
    chunk_delay: float
    channel: int                    # RFCOMM channel; 0 = try them until one answers
    feed: int                       # ~4 mm lines fed after a print, unless the settings say otherwise
    names: tuple[str, ...]          # what it calls itself over Bluetooth (start of the name)
    papers: tuple[tuple[int, Paper], ...] = ()   # (roll width in mm, how a print sits on it); `paper` is the default

    def paper_for(self, mm) -> Paper:
        """The paper for a roll width in mm (None or "": the default)."""
        if mm in (None, ""):
            return self.paper
        sizes = dict(self.papers)
        try:
            return sizes[int(mm)]
        except (KeyError, ValueError):
            known = ", ".join(str(k) for k in sizes) or "none"
            raise ValueError(f"the {self.label} takes paper {known} (mm), not {mm}") from None

    def paper_mm(self, paper: Paper) -> int | None:
        return next((mm for mm, p in self.papers if p == paper), None)


CTP500 = Model("ctp500", "CTP500", "CTP500", Paper(384, 203), ("auto", "cat", "escpos"), 180, 0.010, 1, 2,
               ("CTP500", "CTP-500", "Mini Printer", "CorePrint", "Core Print", "Teal Printer", "Purple Printer",
                "B Pink Printer", "Cherry Printer", "Floral Printer", "Check Printer", "Smiley Printer",
                "Stone Printer", "P Pink Printer", "YHK"))
# Phomemo M02 Pro: 300 dpi, a 576-dot head (48.8 mm), Classic Bluetooth. Pacing, layout and names from
# TiMini-Print's phomemo_m02_pro profile; phomemo-tools and theacodes/phomemo_m02s agree on the commands.
# Paper: on a 53 mm roll the whole head prints. A 50 mm roll (Phomemo's sticker rolls: a ~50 mm face on a 53 mm
# backing) is barely wider than the head and wanders ~1 mm sideways between prints (measured 2026-11-11: its face
# ran from 2.1 to 1.3 mm left of dot 0 and from dot ~559 to ~575 on the right), so prints there use 536 dots
# (45.4 mm) a little left of the middle, which keeps ~2 mm of paper on each side. 50 is the default: a narrower
# print on 53 mm paper loses a few mm, a full-width one on 50 mm paper loses its right edge.
M02PRO_53 = Paper(576, 300)
M02PRO_50 = Paper(536, 300, head=576, left=4)
M02PRO = Model("m02pro", "Phomemo M02 Pro", "M02 Pro", M02PRO_50, ("phomemo",), 128, 0.020, 0, 0,
               ("M02 Pro", "M02PRO", "M02-Pro"), ((50, M02PRO_50), (53, M02PRO_53)))

MODELS = {m.key: m for m in (CTP500, M02PRO)}
DEFAULT_PAPER = CTP500.paper


def model(key: str | None) -> Model:
    key = (key or "ctp500").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    key = {"phomemo": "m02pro", "phomemom02pro": "m02pro"}.get(key, key)
    if key not in MODELS:
        raise ValueError(f"unknown printer model '{key}' (known: {', '.join(MODELS)})")
    return MODELS[key]


def guess(name: str) -> Model | None:
    """The model a Bluetooth name belongs to, or None."""
    low = (name or "").lower()
    for m in (M02PRO, CTP500):          # the specific names first
        if any(low.startswith(h.lower()) or h.lower() in low for h in m.names):
            return m
    return None
