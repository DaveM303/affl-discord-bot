"""Renders the free agency compensation chart as a PNG grid image (Age rows
x OVR columns, each cell showing its compensation band, color-coded) - a
visual overhaul of the old /compensationtable text output, which split the
chart into three separate ASCII code-block tables (70-79/80-89/90-99 OVR)
that were hard to scan as one picture. Modeled directly on ladder_image.py's
approach and constants (same font, same "wide image, bold text" philosophy),
but standalone rather than importing from it, since the two grids don't
share any row/column concepts worth abstracting over.

Pure rendering logic - no discord.py/DB access here. The caller
(free_agency_commands.py) queries compensation_chart and expands it into
{(age, ovr): band} before calling render_compensation_chart_image.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_BOLD_FONT_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")

_CELL_SIZE = 34
_HEADER_CELL_HEIGHT = 34
_AGE_COL_WIDTH = 60

_BG_COLOR = (255, 255, 255)
_HEADER_BG = (0, 0, 0)
_HEADER_TEXT_COLOR = (255, 255, 255)
_TEXT_COLOR = (20, 22, 25)
_NO_COMP_BG = (128, 128, 128)  # gray - outside the chart entirely (no compensation)

# Grid lines drawn on top of the cells (not a gap between them) - every 3rd
# row/column boundary is thicker, so the eye can jump straight to "3 OVR
# points over" or "3 years down" without having to count every cell, the
# same way graph paper uses a bolder line every N squares.
_GRID_LINE_COLOR = (255, 255, 255)
_GRID_LINE_THIN = 1
_GRID_LINE_THICK = 3
_GRID_LINE_MAJOR_EVERY = 3

# Compensation band colors, band 1 (best/most valuable) -> band 5 (least) -
# matches the reference image: green through yellow/orange to red-pink.
_BAND_COLORS = {
    1: (70, 176, 108),
    2: (168, 209, 108),
    3: (247, 220, 111),
    4: (245, 166, 107),
    5: (240, 128, 128),
}


def _load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def render_compensation_chart_image(band_by_age_ovr, ages, ovrs):
    """Draws the compensation chart as a PNG (returned as a BytesIO, ready
    for discord.File) - one row per age, one column per OVR, each cell
    filled with its band's color (or gray if that (age, ovr) combination
    isn't in band_by_age_ovr at all, meaning no compensation applies) and
    labeled with the band number (blank for the gray/no-comp cells, same as
    the reference image). band_by_age_ovr is {(age, ovr): band_int} -
    already fully expanded from compensation_chart's ranges by the caller.
    ages/ovrs are the sorted lists of row/column values to render, letting
    the caller decide exactly what range to show rather than this module
    guessing at chart bounds."""
    header_font = _load_font(_BOLD_FONT_PATH, 15)
    cell_font = _load_font(_BOLD_FONT_PATH, 15)

    width = _AGE_COL_WIDTH + _CELL_SIZE * len(ovrs)
    height = _HEADER_CELL_HEIGHT + _CELL_SIZE * len(ages)

    img = Image.new("RGB", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Corner + OVR header row.
    draw.rectangle([0, 0, _AGE_COL_WIDTH, _HEADER_CELL_HEIGHT], fill=_HEADER_BG)
    draw.text(
        (_AGE_COL_WIDTH / 2, _HEADER_CELL_HEIGHT / 2), "Age/OVR",
        font=_load_font(_BOLD_FONT_PATH, 12), fill=_HEADER_TEXT_COLOR, anchor="mm",
    )
    for col, ovr in enumerate(ovrs):
        x = _AGE_COL_WIDTH + col * _CELL_SIZE
        draw.rectangle([x, 0, x + _CELL_SIZE, _HEADER_CELL_HEIGHT], fill=_HEADER_BG)
        draw.text((x + _CELL_SIZE / 2, _HEADER_CELL_HEIGHT / 2), str(ovr), font=header_font, fill=_HEADER_TEXT_COLOR, anchor="mm")

    # Age header column + data cells - drawn as solid, edge-to-edge blocks
    # (no gap between them); the grid lines below are drawn ON TOP as a
    # separate pass, so their thickness can vary independently of the cell
    # fills underneath.
    for row, age in enumerate(ages):
        y = _HEADER_CELL_HEIGHT + row * _CELL_SIZE
        draw.rectangle([0, y, _AGE_COL_WIDTH, y + _CELL_SIZE], fill=_HEADER_BG)
        draw.text((_AGE_COL_WIDTH / 2, y + _CELL_SIZE / 2), str(age), font=header_font, fill=_HEADER_TEXT_COLOR, anchor="mm")

        for col, ovr in enumerate(ovrs):
            x = _AGE_COL_WIDTH + col * _CELL_SIZE
            band = band_by_age_ovr.get((age, ovr))
            cell_color = _BAND_COLORS.get(band, _NO_COMP_BG)
            draw.rectangle([x, y, x + _CELL_SIZE, y + _CELL_SIZE], fill=cell_color)
            if band is not None:
                draw.text((x + _CELL_SIZE / 2, y + _CELL_SIZE / 2), str(band), font=cell_font, fill=_TEXT_COLOR, anchor="mm")

    # Grid lines over the data cells only (not the black header band) -
    # every _GRID_LINE_MAJOR_EVERY-th boundary drawn thicker, everything
    # else thin, so the eye can jump "5 OVR over" / "5 years down" without
    # counting every cell.
    grid_top = _HEADER_CELL_HEIGHT
    grid_bottom = height
    grid_left = _AGE_COL_WIDTH
    grid_right = width

    for col in range(len(ovrs) + 1):
        x = _AGE_COL_WIDTH + col * _CELL_SIZE
        line_width = _GRID_LINE_THICK if col % _GRID_LINE_MAJOR_EVERY == 0 else _GRID_LINE_THIN
        draw.line([x, grid_top, x, grid_bottom], fill=_GRID_LINE_COLOR, width=line_width)

    for row in range(len(ages) + 1):
        y = _HEADER_CELL_HEIGHT + row * _CELL_SIZE
        line_width = _GRID_LINE_THICK if row % _GRID_LINE_MAJOR_EVERY == 0 else _GRID_LINE_THIN
        draw.line([grid_left, y, grid_right, y], fill=_GRID_LINE_COLOR, width=line_width)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
