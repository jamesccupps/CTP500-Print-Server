"""A tiny note format, so a plain string can carry some layout (handy from Home Assistant and scripts).

    # Big centered heading          ## Smaller heading        ### Smallest
    - item                          bulleted list
    - [ ] todo   /  - [x] done      checklist
    ---   /   - - -   /   ===       line, dashed line, thick line
    > Note                          white text on a black bar
    **Whole line bold**
    Milk ........ $3.49             row: left text, dotted leader, right text
    [qr] https://example.com        QR code
    [banner] HAPPY BIRTHDAY         huge sideways text along the paper
    [center] [right] [big] [small] [huge] [mono] [serif] [bold]   tags for the line after them
    (blank line)                    a little space

Anything else prints as normal text. {date}, {time}, {datetime} and {weekday} are filled in.
"""

from __future__ import annotations

import re

_TAG = re.compile(r"^\[(center|right|left|big|small|huge|mono|serif|bold|qr|banner)\]\s*", re.I)
_CHECK = re.compile(r"^\s*[-*]\s+\[( |x|X)?\](?:\s+(.*))?$")
_BULLET = re.compile(r"^\s*[-*•](?:\s+(.*))?$")
_LEADER = re.compile(r"^(.*?\S)\s*\.{4,}\s*(\S.*)$")
_SIZES = {"small": 18, "big": 32, "huge": 48}


def _tags(line: str) -> tuple[set[str], str]:
    tags = set()
    while True:
        m = _TAG.match(line)
        if not m:
            return tags, line
        tags.add(m.group(1).lower())
        line = line[m.end():]


def _style(tags: set[str], base: dict) -> dict:
    block = dict(base)
    for size_tag, size in _SIZES.items():
        if size_tag in tags:
            block["size"] = size
    for align in ("center", "right", "left"):
        if align in tags:
            block["align"] = align
    if "mono" in tags:
        block["font"] = "mono"
    if "serif" in tags:
        block["font"] = "serif"
    if "bold" in tags:
        block["bold"] = True
    return block


def parse(text: str, size: int = 24, font: str = "sans") -> list[dict]:
    blocks: list[dict] = []
    para: list[str] = []
    para_style: dict | None = None

    def flush():
        nonlocal para, para_style
        if para:
            block = {"type": "text", "text": "\n".join(para), "size": size, "font": font}
            block.update(para_style or {})
            blocks.append(block)
        para, para_style = [], None

    def add_item(kind: str, item):
        if blocks and blocks[-1]["type"] == kind and not para:
            blocks[-1]["items"].append(item)
        else:
            flush()
            blocks.append({"type": kind, "items": [item], "size": size, "font": font})

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            blocks.append({"type": "space", "height": size // 2})
            continue
        if stripped in ("---", "***", "___"):
            flush()
            blocks.append({"type": "line"})
            continue
        if stripped in ("- - -", "-  -  -"):
            flush()
            blocks.append({"type": "line", "style": "dashed"})
            continue
        if stripped == "===":
            flush()
            blocks.append({"type": "line", "thickness": 4})
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush()
            tags, rest = _tags(heading.group(2))
            block = _style(tags, {"type": "heading", "level": len(heading.group(1)), "text": rest, "font": font})
            blocks.append(block)
            continue
        m = _CHECK.match(line)
        if m:
            add_item("checklist", {"text": m.group(2) or "", "done": (m.group(1) or "").lower() == "x"})
            continue
        m = _BULLET.match(line)
        if m:
            add_item("list", m.group(1) or "")
            continue
        if stripped.startswith(">"):
            flush()
            tags, rest = _tags(stripped[1:].strip())
            blocks.append(_style(tags, {"type": "text", "text": rest, "invert": True, "bold": True,
                                        "size": size, "font": font, "align": "center"}))
            continue
        tags, rest = _tags(stripped)
        if "qr" in tags:
            flush()
            blocks.append(_style(tags - {"qr"}, {"type": "qr", "data": rest}))
            continue
        if "banner" in tags:
            flush()
            blocks.append(_style(tags - {"banner"}, {"type": "banner", "text": rest, "font": font}))
            continue
        bold = False
        if len(rest) > 4 and rest.startswith("**") and rest.endswith("**"):
            rest, bold = rest[2:-2], True
        m = _LEADER.match(rest)
        if m:
            flush()
            blocks.append(_style(tags, {"type": "row", "left": m.group(1), "right": m.group(2), "leader": True,
                                        "size": size, "font": font, "bold": bold}))
            continue
        style = _style(tags, {"bold": True} if bold else {})
        if tags or bold:
            flush()
            block = {"type": "text", "text": rest, "size": size, "font": font}
            block.update(style)
            blocks.append(block)
            continue
        if para_style:
            flush()
        para.append(line)
    flush()
    while blocks and blocks[-1]["type"] == "space":
        blocks.pop()
    while blocks and blocks[0]["type"] == "space":
        blocks.pop(0)
    return blocks
