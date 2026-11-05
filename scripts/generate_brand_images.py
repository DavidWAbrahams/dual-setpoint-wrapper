# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""
Generate the brand images for the Dual Setpoint Climate integration.

Draws a thermostat dial with two setpoint ticks: the track to the left of
the lower setpoint is red (heating), the track to the right of the higher
setpoint is blue (cooling). Outputs the eight PNGs Home Assistant serves
from ``custom_components/dual_setpoint/brand/``:

    icon.png / icon@2x.png            logo.png / logo@2x.png
    dark_icon.png / dark_icon@2x.png  dark_logo.png / dark_logo@2x.png

Run from the repository root:  python scripts/generate_brand_images.py
"""

from __future__ import annotations

from io import BytesIO
from math import cos, radians, sin
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Logical (1x) icon size and where HA expects the images.
ICON_SIZE = 128
LOGO_SIZE = (250, 100)
BRAND_DIR = Path("custom_components/dual_setpoint/brand")

# Dial geometry: a 270-degree gauge with the gap at the bottom. PIL angles
# start at three o'clock and increase clockwise, so the gauge sweeps from
# 135 (south-west) through 270 (north) to 405 (south-east).
ARC_START = 135
ARC_SWEEP = 270
LOW_FRACTION = 0.38
HIGH_FRACTION = 0.62

# Floor for the logo text auto-fit loop.
MIN_FONT_PX = 8

RED = (228, 72, 77)
BLUE = (3, 169, 244)
NEUTRAL_LIGHT = (189, 189, 189)
NEUTRAL_DARK = (97, 97, 97)
INK_LIGHT = (66, 66, 66)
INK_DARK = (236, 236, 236)


def _point(
    cx: float, cy: float, radius: float, angle_deg: float
) -> tuple[float, float]:
    """Return the x/y of a point on a circle at a PIL angle (0 = east, CW)."""
    rad = radians(angle_deg)
    return (cx + radius * cos(rad), cy + radius * sin(rad))


def _draw_dial(
    draw: ImageDraw.ImageDraw,
    size: int,
    *,
    neutral: tuple[int, int, int],
    ink: tuple[int, int, int],
) -> None:
    """Render the gauge into a square RGBA image canvas of the given size."""
    width = int(size * 0.085)  # arc thickness
    radius = size * 0.36
    cx = cy = size / 2
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]

    low_angle = ARC_START + ARC_SWEEP * LOW_FRACTION
    high_angle = ARC_START + ARC_SWEEP * HIGH_FRACTION
    end_angle = ARC_START + ARC_SWEEP

    # Heating zone, neutral dead-band, cooling zone.
    draw.arc(bbox, ARC_START, low_angle, fill=RED, width=width)
    draw.arc(bbox, low_angle, high_angle, fill=neutral, width=width)
    draw.arc(bbox, high_angle, end_angle, fill=BLUE, width=width)

    # Setpoint ticks, drawn across the arc.
    tick_in = radius - width * 1.15
    tick_out = radius + width * 1.15
    for angle in (low_angle, high_angle):
        draw.line(
            [
                _point(cx, cy, tick_in, angle),
                _point(cx, cy, tick_out, angle),
            ],
            fill=ink,
            width=int(size * 0.045),
        )

    # Needle pointing at the midpoint (north) and a hub.
    needle_len = radius * 0.62
    draw.line(
        [_point(cx, cy, needle_len, 270), (cx, cy)],
        fill=ink,
        width=int(size * 0.05),
    )
    hub = size * 0.055
    draw.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=ink)


def render_icon(size: int, *, dark: bool) -> Image.Image:
    """Render a square dial icon at ``size`` logical pixels (supersampled)."""
    scale = 4
    img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    _draw_dial(
        ImageDraw.Draw(img),
        size * scale,
        neutral=NEUTRAL_DARK if dark else NEUTRAL_LIGHT,
        ink=INK_DARK if dark else INK_LIGHT,
    )
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _load_font(px: int) -> ImageFont.ImageFont:
    """Return a bold-ish UI font, falling back through common Windows faces."""
    for candidate in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(candidate, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    px: int,
    max_width: int,
) -> ImageFont.ImageFont:
    """Return the largest font up to ``px`` that renders text within max_width."""
    while px > MIN_FONT_PX:
        font = _load_font(px)
        if draw.textlength(text, font=font) <= max_width:
            return font
        px = int(px * 0.92)
    return _load_font(px)


def render_logo(size: tuple[int, int], *, dark: bool) -> Image.Image:
    """Render a rectangular lockup: the dial glyph plus the name."""
    scale = 2
    width, height = size[0] * scale, size[1] * scale
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    dial = render_icon(height, dark=dark)
    img.alpha_composite(dial, (0, 0))

    draw = ImageDraw.Draw(img)
    ink = INK_DARK if dark else INK_LIGHT
    x = int(width * 0.4)
    margin = int(width * 0.03)
    title = "Dual Setpoint"
    font = _fit_font(draw, title, int(height * 0.24), width - x - margin)
    small = _fit_font(draw, "Climate", int(height * 0.15), width - x - margin)

    # Stack title above subtitle and center the text block vertically.
    gap = int(height * 0.06)
    title_ascent, title_descent = font.getmetrics()
    sub_ascent, sub_descent = small.getmetrics()
    block = title_ascent + title_descent + gap + sub_ascent + sub_descent
    top = (height - block) // 2
    draw.text((x, top), title, font=font, fill=ink)
    draw.text(
        (x, top + title_ascent + title_descent + gap), "Climate", font=small, fill=ink
    )
    return img.resize(size, Image.Resampling.LANCZOS)


def _png_bytes(img: Image.Image) -> bytes:
    """Return the PNG encoding of an image."""
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    """Write all eight brand PNGs into the integration's brand directory."""
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for dark, suffix in ((False, ""), (True, "dark_")):
        (BRAND_DIR / f"{suffix}icon.png").write_bytes(
            _png_bytes(render_icon(ICON_SIZE, dark=dark))
        )
        (BRAND_DIR / f"{suffix}icon@2x.png").write_bytes(
            _png_bytes(render_icon(ICON_SIZE * 2, dark=dark))
        )
        (BRAND_DIR / f"{suffix}logo.png").write_bytes(
            _png_bytes(render_logo(LOGO_SIZE, dark=dark))
        )
        (BRAND_DIR / f"{suffix}logo@2x.png").write_bytes(
            _png_bytes(
                render_logo((LOGO_SIZE[0] * 2, LOGO_SIZE[1] * 2), dark=dark),
            ),
        )
    print(f"wrote 8 brand images to {BRAND_DIR}")


if __name__ == "__main__":
    main()
