"""Renders the ladder as a PNG image instead of a Discord code-block table -
built after the text/box-drawn versions (season_commands.py's earlier
format_ladder_table/format_ladder_emoji_key) turned out unsatisfying: no
real column alignment control, and team emojis couldn't render inside a
code block at all. An image sidesteps both - real pixel-level layout, and
each team's actual Discord emoji pasted in as a small icon next to their
name.

Pure rendering logic - no discord.py Interaction/Embed handling here, that
stays in season_commands.py. Only discord.py dependency is Emoji.read()
(async, fetches the emoji's image bytes via the bot's own HTTP client) -
called by the caller BEFORE render_ladder_image, since Pillow drawing
itself is synchronous CPU work with no need to be async.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_BOLD_FONT_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")

# Layout constants - tuned wide-and-short on purpose: Discord's desktop
# client caps a displayed image's HEIGHT more aggressively than its width,
# so a tall image (many rows) gets scaled down and looks small even at a
# generous pixel width. The ladder itself is split into two images (top
# half / bottom half - see post_ladder in season_commands.py) specifically
# so each one can afford a taller row height without the WHOLE ladder's
# total height blowing past that cap - more room per row means a bigger,
# clearer team emoji.
_ROW_HEIGHT = 56
_HEADER_HEIGHT = 38
_PADDING_X = 20
_ICON_GAP = 10   # space between the icon and the team name text

_ICON_INSET = 6  # gap between the Team cell's left edge and the icon itself (also its vertical margin - see _ICON_SIZE), was flush against the colored fill's edge
_ICON_SIZE = _ROW_HEIGHT - 2 * _ICON_INSET  # as large as fits fully inside the row with an even margin top and bottom - never clipped

_POS_COL_WIDTH = 50
_MIN_NAME_COL_WIDTH = 220  # floor for the Team column's text portion (excludes the icon) - widened per-render if any team name needs more (see _build_columns)
_NAME_COL_PADDING = 20     # breathing room after the longest name, so text never touches the next column's edge
_STAT_COL_WIDTH = 56   # W / L / D
_PLAYED_COL_WIDTH = 50  # P
_SCORE_COL_WIDTH = 70  # PF / PA
_PCT_COL_WIDTH = 82    # %
_PTS_COL_WIDTH = 64    # Pts (premiership points)
_FORM_COL_WIDTH = 130  # Form - up to 5 letters (e.g. "WWLWD")
_TEAM_COL_INDEX = 1  # index of "Team" within the columns list built by _build_columns - the only left-aligned column


def _build_columns(ranked_ladder, name_font):
    """(label, width) in display order - Team's width is sized to fit
    the longest actual team name in THIS ladder (never smaller than
    _MIN_NAME_COL_WIDTH), so a long name like "North Melbourne" can never
    overflow into the next column. The icon reserves _ICON_INSET +
    _ICON_SIZE + _ICON_GAP more on top of this column's text-only width."""
    longest_name_width = max(
        (name_font.getlength(row.team_name) for row in ranked_ladder), default=0
    )
    name_col_width = max(_MIN_NAME_COL_WIDTH, int(longest_name_width) + _ICON_INSET + _ICON_SIZE + _ICON_GAP + _NAME_COL_PADDING)

    return [
        ("Pos", _POS_COL_WIDTH),
        ("Team", name_col_width),
        ("P", _PLAYED_COL_WIDTH),
        ("Pts", _PTS_COL_WIDTH),
        ("%", _PCT_COL_WIDTH),
        ("W", _STAT_COL_WIDTH),
        ("L", _STAT_COL_WIDTH),
        ("D", _STAT_COL_WIDTH),
        ("PF", _SCORE_COL_WIDTH),
        ("PA", _SCORE_COL_WIDTH),
        ("Form", _FORM_COL_WIDTH),
    ]

# Light theme.
_BG_COLOR = (255, 255, 255)
_HEADER_BG = (0, 0, 0)
_ROW_BG_EVEN = (247, 248, 250)
_ROW_BG_ODD = (255, 255, 255)
_TEXT_COLOR = (32, 34, 38)
_HEADER_TEXT_COLOR = (255, 255, 255)
_BORDER_COLOR = (223, 226, 231)
_COLUMN_DIVIDER_COLOR = (90, 90, 90)  # divider lines cross the black header too, so they need to stay visible there


def _load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


async def fetch_team_icons(bot, emoji_by_team):
    """Downloads each team's Discord emoji image (via discord.py's own
    Emoji.read(), so no extra HTTP dependency needed) and decodes it into a
    Pillow Image, resized to fit within _ICON_SIZE (preserving aspect
    ratio, since emojis aren't always square - a naive square resize is
    what made icons look squished). Returns {team_id: PIL.Image}, silently
    omitting any team with no emoji configured or whose emoji image
    couldn't be fetched/decoded (a missing icon just means that row
    renders without one, never a crash). Must be called BEFORE
    render_ladder_image, since fetching is async and drawing isn't."""
    from utils import get_team_emoji

    icons = {}
    for team_id, emoji_id in emoji_by_team.items():
        emoji = get_team_emoji(bot, emoji_id)
        if emoji is None:
            continue
        try:
            raw = await emoji.read()
            icon = Image.open(io.BytesIO(raw)).convert("RGBA")
            icon.thumbnail((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
            icons[team_id] = icon
        except Exception:
            continue
    return icons


def _column_x_positions(columns):
    """Left-edge x of each column's content area (inside its own padding),
    in the same order as `columns` - computed once from a running offset so
    adding/removing/reordering columns never requires touching any of the
    per-column drawing math below."""
    positions = []
    x = _PADDING_X
    for _, width in columns:
        positions.append(x)
        x += width
    return positions, x + _PADDING_X  # (positions, total width)


def _hex_to_rgb(hex_color):
    """Parses a stored 6-digit hex string (no leading '#', e.g. "1E5C3A")
    into an (r, g, b) tuple. Returns None for anything malformed rather
    than raising, since a bad/missing color should just mean "no special
    styling for this row", never a crash."""
    if not hex_color or len(hex_color) != 6:
        return None
    try:
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _relative_luminance(rgb):
    # Standard perceptual luminance weighting - used only to pick a
    # legible black/white fallback text color against an arbitrary
    # background, not for any color-accuracy-sensitive purpose.
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _readable_text_color(background_rgb):
    """Black or white, whichever contrasts better against background_rgb -
    the fallback used for the team name when no secondary color is set."""
    return (20, 22, 25) if _relative_luminance(background_rgb) > 150 else (255, 255, 255)


def split_ladder_for_images(ranked_ladder):
    """Splits a full ranked ladder into two even-ish halves (the first half
    gets the extra team on an odd-sized ladder) - each rendered as its own
    image (see render_ladder_image's start_position param) so every row can
    afford a taller height / bigger team emoji without the WHOLE ladder's
    total image height blowing past Discord's display height cap. Returns
    (top_half, bottom_half) - either half may be empty for a tiny ladder,
    which the caller should just skip posting."""
    midpoint = (len(ranked_ladder) + 1) // 2
    return ranked_ladder[:midpoint], ranked_ladder[midpoint:]


def render_ladder_image(ranked_ladder, team_icons, primary_color_by_team=None, secondary_color_by_team=None, start_position=1, show_header=True):
    """Draws the ladder as a PNG (returned as a BytesIO, ready for
    discord.File) - one row per team, with Pos/Team (+ icon, if team_icons
    has one for that team_id)/P/Pts/%/W/L/D/PF/PA/Form columns (see
    _build_columns for the authoritative order), alternating row shading,
    and a header bar. No title bar - the round/season label is posted as a
    separate plain-text message alongside the image (see post_ladder in
    season_commands.py), not baked into the image itself. If a team has a
    primary color configured (primary_color_by_team, {team_id: "RRGGBB"
    hex string}), their Team cell (icon + name) is filled with it; the team
    name is then drawn in their secondary color if set
    (secondary_color_by_team, same shape), or otherwise whichever of
    black/white contrasts better against the primary fill - never a fixed
    color, since an arbitrary team color could make a fixed text color
    unreadable. team_icons is {team_id: PIL.Image} from fetch_team_icons -
    purely synchronous, safe to call directly (no network access, no
    discord.py objects touched).

    ranked_ladder need not be the WHOLE ladder - start_position lets the
    caller pass just a slice (see split_ladder_for_images) while still
    showing each team's real, absolute ladder position rather than
    restarting the Pos column at 1 for a second-half image. show_header
    controls whether the Pos/Team/W/L/... column-header bar is drawn at
    all - the bottom-half image (already following directly under the top
    half's own header) passes False to skip a second, redundant one."""
    primary_color_by_team = primary_color_by_team or {}
    secondary_color_by_team = secondary_color_by_team or {}

    header_font = _load_font(_BOLD_FONT_PATH, 17)
    name_font = _load_font(_BOLD_FONT_PATH, 19)
    stat_font = _load_font(_BOLD_FONT_PATH, 19)

    columns = _build_columns(ranked_ladder, name_font)
    col_x, width = _column_x_positions(columns)
    header_height = _HEADER_HEIGHT if show_header else 0
    height = header_height + _ROW_HEIGHT * len(ranked_ladder)

    img = Image.new("RGB", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    header_y = 0
    if show_header:
        draw.rectangle([0, header_y, width, header_y + _HEADER_HEIGHT], fill=_HEADER_BG)
        for i, (label, col_width) in enumerate(columns):
            x = col_x[i]
            if i == _TEAM_COL_INDEX:
                # Centered over the icon+name span (not the whole column,
                # most of which is empty space reserved for longer team
                # names), so "Team" sits above the actual icon/text
                # content, not floating to its left.
                content_width = _ICON_INSET + _ICON_SIZE + _ICON_GAP + name_font.getlength(max((row.team_name for row in ranked_ladder), key=len, default=""))
                content_width = min(content_width, col_width)
                draw.text((x + content_width / 2, header_y + _HEADER_HEIGHT / 2), label, font=header_font, fill=_HEADER_TEXT_COLOR, anchor="mm")
            else:
                draw.text((x + col_width / 2, header_y + _HEADER_HEIGHT / 2), label, font=header_font, fill=_HEADER_TEXT_COLOR, anchor="mm")
        draw.line([0, header_y + _HEADER_HEIGHT, width, header_y + _HEADER_HEIGHT], fill=_BORDER_COLOR, width=1)

    row_y = header_y + header_height
    for position, row in enumerate(ranked_ladder, start=start_position):
        row_bg = _ROW_BG_EVEN if position % 2 == 0 else _ROW_BG_ODD
        draw.rectangle([0, row_y, width, row_y + _ROW_HEIGHT], fill=row_bg)
        mid_y = row_y + _ROW_HEIGHT / 2

        played = row.wins + row.losses + row.draws
        pct = "∞" if row.percentage == float("inf") else f"{row.percentage:.1f}"
        # Keyed by column label (not position) so reordering _COLUMNS above
        # never silently misaligns which value lands in which column.
        value_by_label = {
            "Pos": str(position),
            "P": str(played),
            "Pts": str(row.premiership_points),
            "%": pct,
            "W": str(row.wins),
            "L": str(row.losses),
            "D": str(row.draws),
            "PF": str(row.points_for),
            "PA": str(row.points_against),
            "Form": "".join(row.form),
        }

        primary_color = _hex_to_rgb(primary_color_by_team.get(row.team_id))

        for i, (label, col_width) in enumerate(columns):
            x = col_x[i]
            if i == _TEAM_COL_INDEX:
                if primary_color is not None:
                    # Fill the WHOLE Team cell (icon + name span), not just
                    # a thin accent - this is the actual request: the
                    # team's color should fill the cell, not just hint at
                    # it. Confined to this one column so every other
                    # column's already-dark text stays readable regardless
                    # of how bright/saturated an arbitrary team color is.
                    draw.rectangle([x, row_y, x + col_width, row_y + _ROW_HEIGHT], fill=primary_color)
                    name_color = _hex_to_rgb(secondary_color_by_team.get(row.team_id)) or _readable_text_color(primary_color)
                else:
                    name_color = _TEXT_COLOR

                icon = team_icons.get(row.team_id)
                text_x = x + _ICON_INSET
                if icon is not None:
                    # Icon slot is a fixed _ICON_SIZE square, inset from the
                    # cell's left edge by _ICON_INSET (was flush against it,
                    # sitting too close to the colored fill's border) -
                    # regardless of the actual (possibly non-square, since
                    # thumbnail() preserves aspect ratio) icon dimensions,
                    # so the text start position never shifts per-row.
                    # _ICON_SIZE is sized to fit fully within the row (see
                    # its definition), so the icon is simply centered both
                    # ways within its slot - never clipped.
                    icon_x = int(x + _ICON_INSET + (_ICON_SIZE - icon.width) / 2)
                    icon_y = int(mid_y - icon.height / 2)
                    img.paste(icon, (icon_x, icon_y), icon)
                    text_x = x + _ICON_INSET + _ICON_SIZE + _ICON_GAP
                draw.text((text_x, mid_y), row.team_name, font=name_font, fill=name_color, anchor="lm")
            else:
                draw.text((x + col_width / 2, mid_y), value_by_label[label], font=stat_font, fill=_TEXT_COLOR, anchor="mm")

        row_y += _ROW_HEIGHT

    draw.line([0, row_y, width, row_y], fill=_BORDER_COLOR, width=1)

    # Vertical divider lines, spanning the full table (header through the
    # last row) - both sides of the Team column, and between %/W and PA/Form
    # (looked up by label so these stay correct if columns are reordered).
    table_top = header_y
    table_bottom = row_y
    label_to_index = {label: i for i, (label, _) in enumerate(columns)}
    divider_after_labels = ["Team", "%", "PA"]
    for label in divider_after_labels:
        i = label_to_index[label]
        divider_x = col_x[i] + columns[i][1]
        draw.line([divider_x, table_top, divider_x, table_bottom], fill=_COLUMN_DIVIDER_COLOR, width=1)
    team_left_x = col_x[_TEAM_COL_INDEX]
    draw.line([team_left_x, table_top, team_left_x, table_bottom], fill=_COLUMN_DIVIDER_COLOR, width=1)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
