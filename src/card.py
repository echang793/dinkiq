"""Shareable Instagram-story-sized stat card (PNG) — a lighter, social-first
sibling to report.py's PDF. Pure function of already-loaded session JSON, no
filesystem access, same reasoning as report.py: cheap and safe to render on
every request.
"""

import io

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
BG = (14, 20, 32)
ACCENT = (242, 193, 78)
WHITE = (240, 240, 245)
MUTED = (150, 158, 176)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (W - (bbox[2] - bbox[0])) / 2
    draw.text((x, y), text, font=font, fill=fill)


def render_stat_card(meta: dict, metrics: dict, events: dict,
                     points: dict, dupr: dict) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    _center(d, 90, "DINKIQ", _font(48), ACCENT)
    title = meta.get("label") or meta.get("filename") or "Match analysis"
    _center(d, 160, title[:40], _font(30), WHITE)
    if meta.get("played_at"):
        _center(d, 205, meta["played_at"], _font(22), MUTED)

    if dupr and dupr.get("available"):
        _center(d, 420, f"~{dupr['band']:g}", _font(220), ACCENT)
        conf = int(round(dupr.get("confidence", 0) * 100))
        _center(d, 660, f"estimated DUPR · {conf}% confidence", _font(24), MUTED)
    else:
        _center(d, 500, "Skill estimate unavailable", _font(30), MUTED)

    zp = metrics.get("zone_pct", {})
    stats = [
        ("KITCHEN TIME", f"{zp.get('kitchen', 0):.0f}%"),
        ("DISTANCE", f"{metrics.get('distance_ft', 0):,.0f} ft"),
        ("COVERAGE", f"{metrics.get('coverage_pct', 0):.0f}%"),
    ]
    if points and points.get("points_scored"):
        stats.append(("WIN RATE", f"{points['win_pct']:.0f}%"))
    elif events.get("rally_count"):
        stats.append(("RALLIES", str(events["rally_count"])))

    row_y = 900
    col_w = W // len(stats)
    for i, (label, value) in enumerate(stats):
        cx = col_w * i + col_w // 2
        bbox = d.textbbox((0, 0), value, font=_font(52))
        d.text((cx - (bbox[2] - bbox[0]) / 2, row_y), value, font=_font(52), fill=WHITE)
        bbox = d.textbbox((0, 0), label, font=_font(18))
        d.text((cx - (bbox[2] - bbox[0]) / 2, row_y + 66), label, font=_font(18), fill=MUTED)

    _center(d, H - 80, "dinkiq · video-analyzed pickleball coaching", _font(20), MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
