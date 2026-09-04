"""
warn_x_image
------------
The image that rides along with an @USLayoff post.

A plain text post scrolls past. A card with the employer's name set large, the
headcount larger, and the dashboard's own mark in the corner is what stops a
thumb — so every queued post gets one, generated here.

ON LOGOS, DELIBERATELY. This does NOT fetch or embed a company's actual logo.
Those are trademarks, they are not licensed to us, and republishing one beside
a layoff headline is both a rights problem and a claim of association nobody
granted. What the card carries instead is the employer's NAME — naming the
subject of a factual report is exactly what nominative use protects — set as
the loudest thing on the card, anchored by a generated monogram tile in the
dashboard's own palette. Everything drawn here is ours.

The palette and the descending-arrow motif come from docs/icon.svg, so a post
looks like it came from the same place as the dashboard it links to.

Imports Pillow and nothing from the project. A missing font, an unwritable
directory, or a Pillow that is not installed all degrade to "no image" rather
than failing a post: the text is the story, the card is the packaging.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger("warn_x_image")

# docs/icon.svg's own colours.
BG_TOP = (27, 34, 48)
BG_BOTTOM = (13, 17, 23)
INK = (238, 243, 249)
MUTED = (139, 152, 168)
RULE = (48, 54, 61)
BLUE = (88, 166, 255)
RED = (240, 71, 71)
CORAL = (247, 129, 102)

# Bars behind the arrow, matching the icon's muted series.
BAR_COLORS = [
    (88, 166, 255), (57, 208, 216), (63, 185, 80),
    (210, 153, 34), (188, 140, 255), (247, 129, 102),
]

CARD_W, CARD_H = 1600, 900          # 16:9 — X renders this without cropping
MARGIN = 92

# Font candidates, most-preferred first. macOS ships the first group; the
# DejaVu paths are what a Linux runner has. Nothing here is bundled, so a host
# with none of them still gets a card via Pillow's built-in face.
_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_REG = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _font(paths, size):
    from PIL import ImageFont

    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — a broken face is not fatal
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:      # Pillow < 10 has no size argument
        return ImageFont.load_default()


def _text_w(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def _fit(draw, text, paths, start, min_size, max_w):
    """Largest font at or below ``start`` that fits ``text`` into ``max_w``."""
    size = start
    while size > min_size:
        font = _font(paths, size)
        if _text_w(draw, text, font) <= max_w:
            return font
        size -= 4
    return _font(paths, min_size)


def _wrap(draw, text, font, max_w):
    """Greedy word wrap, so a long employer name never runs off the card."""
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if _text_w(draw, trial, font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def monogram(name: str) -> str:
    """One or two letters standing in for a logo we are not entitled to use."""
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", name or "") if w]
    if not words:
        return "?"
    if len(words[0]) > 1 and words[0].isupper() and len(words) == 1:
        return words[0][:2]                      # "IBM" -> "IB"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _gradient(size):
    from PIL import Image

    w, h = size
    img = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        img.putpixel((0, y), tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        ))
    return img.resize((w, h))


def _draw_motif(draw, x, y, w, h):
    """The dashboard's descending arrow over muted bars, scaled into a box."""
    bar_w = w // 9
    heights = [0.42, 0.62, 0.50, 0.74, 0.56, 0.86]
    for i, frac in enumerate(heights):
        bx = x + i * (bar_w + bar_w // 3)
        bh = int(h * frac)
        draw.rounded_rectangle(
            [bx, y + h - bh, bx + bar_w, y + h], radius=bar_w // 4,
            fill=tuple(round(c * 0.3 + BG_BOTTOM[j] * 0.7)
                       for j, c in enumerate(BAR_COLORS[i])),
        )
    draw.line([x, y + h, x + w, y + h], fill=RULE, width=3)
    pts = [(x + w * 0.02, y + h * 0.10), (x + w * 0.30, y + h * 0.34),
           (x + w * 0.50, y + h * 0.22), (x + w * 0.74, y + h * 0.60),
           (x + w * 0.92, y + h * 0.74)]
    for i in range(len(pts) - 1):
        t = i / (len(pts) - 2)
        colour = tuple(round(BLUE[j] + (RED[j] - BLUE[j]) * t) for j in range(3))
        draw.line([pts[i], pts[i + 1]], fill=colour, width=13, joint="curve")
    ax, ay = pts[-1]
    draw.line([(ax, ay), (ax - w * 0.10, ay + 2)], fill=RED, width=12)
    draw.line([(ax, ay), (ax - 2, ay - h * 0.16)], fill=RED, width=12)


def build_card(display: str, employees, place: str, effective: str,
               states=None, per_state=None, out_path=None):
    """Render one post card. Returns the written Path, or None if it can't.

    ``employees`` may be None — Hawaii and Oklahoma publish no headcount, and a
    card that invented a number would be worse than a card without one.
    """
    try:
        from PIL import ImageDraw
    except ImportError as e:  # noqa: BLE001
        log.warning(f"Pillow not installed ({e}) — posting without a card.")
        return None

    try:
        img = _gradient((CARD_W, CARD_H))
        d = ImageDraw.Draw(img)

        # Top rule + our own mark. This is the only "logo" on the card.
        d.line([MARGIN, 150, CARD_W - MARGIN, 150], fill=RULE, width=2)
        f_mark = _font(_BOLD, 34)
        d.text((MARGIN, 96), "US LAYOFF TRACKER", font=f_mark, fill=INK)
        f_small = _font(_REG, 28)
        label = "WARN notice"
        d.text((CARD_W - MARGIN - _text_w(d, label, f_small), 102),
               label, font=f_small, fill=MUTED)

        # Monogram tile — a logo-shaped anchor that belongs to us.
        tile = 132
        tx, ty = MARGIN, 224
        d.rounded_rectangle([tx, ty, tx + tile, ty + tile], radius=26,
                            fill=(31, 41, 55), outline=RULE, width=2)
        mono = monogram(display)
        f_mono = _fit(d, mono, _BOLD, 74, 34, tile - 30)
        mw = _text_w(d, mono, f_mono)
        d.text((tx + (tile - mw) / 2, ty + tile / 2 - 46), mono,
               font=f_mono, fill=CORAL)

        # The employer's name — the loudest thing on the card.
        name_x = tx + tile + 44
        name_w = CARD_W - MARGIN - name_x
        f_name = _fit(d, display, _BOLD, 92, 40, name_w)
        lines = _wrap(d, display, f_name, name_w)[:2]
        y = ty + (18 if len(lines) > 1 else 40)
        for line in lines:
            d.text((name_x, y), line, font=f_name, fill=INK)
            y += f_name.size + 8

        # The number — the second thing the eye lands on, after the name.
        if employees:
            num = f"{employees:,}"
            f_num = _font(_BOLD, 236)
            d.text((MARGIN, 452), num, font=f_num, fill=INK)
            f_unit = _font(_REG, 46)
            d.text((MARGIN + _text_w(d, num, f_num) + 26, 452 + 168), "jobs",
                   font=f_unit, fill=MUTED)
        else:
            # Hawaii and Oklahoma publish no headcount. A dash here read as a
            # redaction bar, which is worse than saying so in words.
            f_none = _font(_BOLD, 82)
            d.text((MARGIN, 520), "Headcount not", font=f_none, fill=MUTED)
            d.text((MARGIN, 520 + 96), "reported by the state",
                   font=f_none, fill=MUTED)

        _draw_motif(d, CARD_W - MARGIN - 420, 430, 420, 250)

        # Footer: where, when, and the source.
        d.line([MARGIN, 748, CARD_W - MARGIN, 748], fill=RULE, width=2)
        f_foot = _font(_REG, 38)
        f_footb = _font(_BOLD, 38)
        where = place or ", ".join(states or [])
        d.text((MARGIN, 786), where[:52], font=f_footb, fill=INK)
        when = f"Effective {effective}" if effective else "Effective date not reported"
        d.text((MARGIN, 786 + 46), when, font=f_foot, fill=MUTED)
        src = "bilalahamad0.github.io/warn"
        d.text((CARD_W - MARGIN - _text_w(d, src, f_foot), 786 + 23),
               src, font=f_foot, fill=BLUE)

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG", optimize=True)
        return out
    except Exception as e:  # noqa: BLE001 — never let packaging kill a post
        log.warning(f"Card generation failed ({e}) — posting without one.")
        return None
