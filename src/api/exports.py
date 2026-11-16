"""Downloads: CSV of all sessions, highlight reel, PDF report, stat card."""


from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

import media
from api.common import DEFAULT_PLAYER_NAME, compare_summary, get_session_dir, read_artifact, iter_sessions
from session_status import get_status

router = APIRouter()


@router.get("/api/sessions/export.csv")
def export_sessions_csv():
    """Flat CSV of every completed session's summary stats (same fields
    compare_summary already aggregates), for users who want to pivot their
    own history in Sheets/Excel."""
    import csv
    import io

    rows = []
    for d in iter_sessions(newest_first=False, done_only=True):
        row = compare_summary(d)
        row["player"] = read_artifact(d, "meta.json").get("player") or DEFAULT_PLAYER_NAME
        rows.append(row)

    def _csv_safe(v):
        # a cell starting with = + - @ is run as a formula by Excel/Sheets;
        # label/opponent/player are free text (and shared users can set them)
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    rows = [{k: _csv_safe(v) for k, v in row.items()} for row in rows]
    buf = io.StringIO()
    fieldnames = ["session_id", "player", "label", "played_at", "opponent",
                 "kitchen_pct", "transition_pct", "distance_ft", "avg_speed_ft_s",
                 "coverage_pct", "rally_count", "avg_rally_hits", "play_time_pct",
                 "points_won", "points_lost", "win_pct", "dupr_band", "dupr_confidence"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="dinkiq_sessions.csv"'})


@router.get("/api/session/{sid}/highlights.mp4")
def highlights(sid: str):
    """Auto-generated reel of the top rally clips (longest rallies first),
    built on first request and cached until the session is recalibrated."""
    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a highlight reel")
    out = sdir / "highlights.mp4"
    if not out.exists():
        if media.build_highlights(sdir) is None:
            raise HTTPException(404, "no rally clips available for a highlight reel")
    return FileResponse(out, media_type="video/mp4",
                        filename=f"dinkiq_highlights_{sid}.mp4")


@router.get("/api/session/{sid}/report.pdf")
def report_pdf(sid: str):
    from report import build_report_pdf

    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a report")

    meta = read_artifact(sdir, "meta.json")
    pdf = build_report_pdf(meta, read_artifact(sdir, "metrics.json"),
                           read_artifact(sdir, "events.json"),
                           read_artifact(sdir, "shots.json"),
                           read_artifact(sdir, "points.json"),
                           read_artifact(sdir, "dupr.json"))
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="dinkiq_{sid}.pdf"'})


@router.get("/api/session/{sid}/card.png")
def stat_card(sid: str):
    from card import render_stat_card

    sdir = get_session_dir(sid)
    if get_status(sdir).get("stage") != "done":
        raise HTTPException(409, "session not ready for a stat card")

    png = render_stat_card(read_artifact(sdir, "meta.json"), read_artifact(sdir, "metrics.json"),
                           read_artifact(sdir, "events.json"), read_artifact(sdir, "points.json"),
                           read_artifact(sdir, "dupr.json"))
    return Response(content=png, media_type="image/png", headers={
        "Content-Disposition": f'attachment; filename="dinkiq_card_{sid}.png"'})
