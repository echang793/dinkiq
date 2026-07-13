"""One-page PDF session report — the shareable artifact of a fitness-app coach.

Pure function of already-loaded session JSON (no filesystem access), so it's
easy to test and safe to call from the server on every request (cheap, no
staleness risk from caching a file that recalibration would invalidate).
"""

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

ACCENT = colors.HexColor("#5a8f3c")
MUTED = colors.HexColor("#666666")
WARN = colors.HexColor("#b34700")


def _styles():
    ss = getSampleStyleSheet()
    # NOTE: ParagraphStyle inherits `leading` (line height) from its parent, not
    # scaled to a new fontSize — a 34pt Hero built on Normal's 12pt leading
    # renders with massive glyph overflow into whatever follows. Always set
    # leading explicitly (~1.15x fontSize) on any style that bumps fontSize.
    ss.add(ParagraphStyle("H1", parent=ss["Title"], fontSize=22, leading=26,
                          textColor=ACCENT, spaceAfter=2))
    ss.add(ParagraphStyle("Sub", parent=ss["Normal"], textColor=MUTED, fontSize=10,
                          leading=13, spaceAfter=14))
    ss.add(ParagraphStyle("Hero", parent=ss["Normal"], fontSize=34, leading=40,
                          textColor=ACCENT, spaceAfter=4))
    ss.add(ParagraphStyle("HeroLabel", parent=ss["Normal"], fontSize=10,
                          textColor=MUTED))
    ss.add(ParagraphStyle("Section", parent=ss["Heading2"], fontSize=13,
                          spaceBefore=14, spaceAfter=6))
    ss.add(ParagraphStyle("Tip", parent=ss["Normal"], fontSize=10, spaceAfter=6,
                          leftIndent=10))
    ss.add(ParagraphStyle("Caveat", parent=ss["Normal"], fontSize=8.5,
                          textColor=WARN, spaceAfter=3))
    return ss


def _stat_table(pairs: list[tuple[str, str]], cols: int = 3) -> Table:
    rows = [pairs[i:i + cols] for i in range(0, len(pairs), cols)]
    vals = [[f"{v}" for _, v in row] for row in rows]
    labels = [[label for label, _ in row] for row in rows]
    data = []
    for v_row, l_row in zip(vals, labels):
        data.append(v_row)
        data.append(l_row)
    t = Table(data, colWidths=[(letter[0] - 1.6 * inch) / cols] * cols)
    style = [("FONTSIZE", (0, 0), (-1, -1), 9), ("TEXTCOLOR", (0, 0), (-1, -1), MUTED),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2)]
    for r in range(0, len(data), 2):
        style.append(("FONTSIZE", (0, r), (-1, r), 15))
        style.append(("TEXTCOLOR", (0, r), (-1, r), colors.black))
        style.append(("FONTNAME", (0, r), (-1, r), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    t.hAlign = "LEFT"  # Table defaults to CENTER within the frame — don't want that
    return t


def build_report_pdf(meta: dict, metrics: dict, events: dict, shots: dict,
                     points: dict, dupr: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.8 * inch, rightMargin=0.8 * inch)
    ss = _styles()
    story = []

    title = meta.get("label") or meta.get("filename") or "Session report"
    story.append(Paragraph("PickleCoach", ss["H1"]))
    story.append(Paragraph(f"{title} &middot; {meta.get('played_at', '')}", ss["Sub"]))

    if dupr and dupr.get("available"):
        band = dupr["band"]
        conf = int(round(dupr.get("confidence", 0) * 100))
        story.append(Paragraph(f"~{band:g}", ss["Hero"]))
        story.append(Paragraph(
            f"your play resembles this DUPR range &middot; {conf}% confidence",
            ss["HeroLabel"]))
        story.append(Spacer(1, 10))
        dims = sorted(dupr.get("dimensions", {}).values(), key=lambda d: d["band"])
        dim_rows = [[d["label"], f"{d['band']:.1f}"] for d in dims]
        dt = Table(dim_rows, colWidths=[3.2 * inch, 1 * inch])
        dt.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TEXTCOLOR", (1, 0), (1, -1), ACCENT),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        dt.hAlign = "LEFT"
        story.append(dt)
        for c in dupr.get("caveats", []):
            story.append(Paragraph(f"&#9888; {c}", ss["Caveat"]))
    else:
        story.append(Paragraph("Skill estimate not available for this session.",
                               ss["Sub"]))

    story.append(Paragraph("Session stats", ss["Section"]))
    zp = metrics.get("zone_pct", {})
    stats = [
        ("time at kitchen line", f"{zp.get('kitchen', 0):.0f}%"),
        ("distance covered", f"{metrics.get('distance_ft', 0):,.0f} ft"),
        ("court coverage", f"{metrics.get('coverage_pct', 0):.0f}%"),
    ]
    if events.get("rally_count"):
        stats += [
            ("rallies", str(events["rally_count"])),
            ("avg hits / rally", str(events.get("avg_rally_hits", "—"))),
            ("time in play", f"{events.get('play_time_pct', 0):.0f}%"),
        ]
    if points and points.get("points_scored"):
        stats += [
            ("points won-lost", f"{points['points_won']}-{points['points_lost']}"),
            ("win rate", f"{points['win_pct']:.0f}%"),
            ("unforced errors", str(points["unforced_errors"])),
        ]
    story.append(_stat_table(stats))

    if dupr and dupr.get("tips"):
        story.append(Paragraph("Coaching notes", ss["Section"]))
        for tip in dupr["tips"]:
            story.append(Paragraph(f"&bull; {tip}", ss["Tip"]))

    for w in metrics.get("warnings", []):
        story.append(Paragraph(f"&#9888; {w}", ss["Caveat"]))

    doc.build(story)
    return buf.getvalue()
