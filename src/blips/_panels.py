"""Floating panels: the search field, the flight and airport cards, help.

A panel is a block of lines on a solid background, drawn over the scope
on the live loop's overlay channel (cursor-addressed, after the frame), in
the same recipe as the hover chip so they read as one family.  A line is
a list of segments ``(text, rgb)`` or ``(text, rgb, bold)``; ``panel()``
clips each to the panel's width, with an ellipsis where it cut, and pads
the background out to the edge.
"""

from blips._color import BOLD, RESET, bg, fg
from blips._framebuffer import visible_len
from blips._theme import ensure_contrast

_BOLD_OFF = "\033[22m"


def _char_width(ch):
    return visible_len(ch)


def clip(text, width):
    """``text`` cut to ``width`` columns, an ellipsis marking the cut."""
    if visible_len(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def line_width(segs):
    return sum(visible_len(seg[0]) for seg in segs)


def render_line(segs, width, base_bg):
    """One line of segments as ANSI on ``base_bg``, exactly ``width`` wide."""
    parts = [bg(*base_bg)]
    used = 0
    for seg in segs:
        text, rgb = seg[0], seg[1]
        bold = len(seg) > 2 and seg[2]
        room = width - used
        if room <= 0:
            break
        text = clip(text, room)
        col = ensure_contrast(rgb, base_bg, 3.0) if rgb else None
        if bold:
            parts.append(BOLD)
        if col is not None:
            parts.append(fg(*col))
        parts.append(text)
        if bold:
            parts.append(_BOLD_OFF)
        used += visible_len(text)
    if used < width:
        parts.append(" " * (width - used))
    return "".join(parts)


def panel(lines, row, col, width, base_bg, pad=1):
    """Cursor-addressed ANSI for a panel whose top-left is the 1-based
    (col, row): each line padded by ``pad`` columns on both sides."""
    inner = width - 2 * pad
    out = []
    for i, segs in enumerate(lines):
        if isinstance(segs, str):
            segs = [(segs, None)]
        body = render_line(segs, inner, base_bg)
        out.append(f"\033[{row + i};{col}H{bg(*base_bg)}{' ' * pad}{body}"
                   f"{bg(*base_bg)}{' ' * pad}{RESET}")
    return "".join(out)


def fit_width(lines, lo, hi, pad=1):
    """The width a panel needs for ``lines``, kept within [lo, hi]."""
    need = max((line_width(seg if not isinstance(seg, str) else [(seg, None)])
                for seg in lines), default=0) + 2 * pad
    return max(lo, min(hi, need))


def bar(fraction, width, done_rgb, todo_rgb):
    """A progress bar of ``width`` cells as segments."""
    fraction = max(0.0, min(1.0, fraction))
    full = int(round(fraction * width))
    return [("━" * full, done_rgb), ("─" * (width - full), todo_rgb)]
