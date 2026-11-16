"""Analysis stages: events -> shots -> points -> rating, orchestrated by analyze()."""

import logging
import threading
from pathlib import Path

import numpy as np
import pandas as pd

import media
import notify
from court import NET_Y, CourtCalibration, court_px_width
from events import (corroborate_hits, detect_hits, detect_swings,
                    rally_metrics, segment_rallies, wrist_speed)
from jsonio import atomic_write_json, load_json
from metrics import compute_metrics
from pipeline import FPS, MODELS
from points import (MY_TEAM, attribute_hitters, point_summary, rally_outcomes,
                    serve_side_win_rates, shot_type_outcomes)
from session_status import get_status, set_status
from tracking import (count_secondary_court_tracks, pick_opponents, pick_subject,
                      run_tracking, stitch_chain_ids, stitch_subject,
                      subject_court_positions)

log = logging.getLogger("dinkiq.stages")


def _player_swings(tracks: pd.DataFrame, track_id: int | None, court_width_px: float) -> np.ndarray:
    if track_id is None or not len(tracks):
        return np.array([])
    chain = stitch_subject(tracks, track_id)
    return detect_swings(wrist_speed(chain, FPS, court_width_px)) if len(chain) else np.array([])


def events(sdir: Path, tracks: pd.DataFrame, subject: int, partner: int | None,
          opponents: list[int], corners_px: list[list[float]],
          hit_times: np.ndarray | None = None,
          video_duration: float | None = None):
    """M2: paddle hits from audio, rallies, swings, per-rally clips.

    Degrades gracefully: no/quiet audio -> events.json records zero rallies.
    hit_times may be precomputed (analyze runs audio concurrently with tracking).
    `partner` is None in singles; `opponents` has 1 id in singles, up to 2 in
    doubles. Returns (hit_times, team_hits, rallies, hitters) — hitters is a
    per-hit 'subject'/'partner'/'opponent1'/'opponent2'/'unknown' label for
    the points stage; team_hits is True for any hit attributed to subject OR
    partner (used for "my team's shots" downstream, identical to
    subject-only in singles since partner is always None there).

    `video_duration` should be the real ffprobe duration — used as the
    play_time_pct denominator. Falls back to the last tracked frame only
    when the caller doesn't have it (e.g. tests): that fallback can
    under-report true length (players undetected near the end while a
    rally is still audible), which used to let play_time_pct exceed 100%.
    """
    audio = sdir / "audio.wav"
    duration = video_duration if video_duration else (
        float(tracks["frame"].max() + 1) / FPS if len(tracks) else 0.0)
    if hit_times is None:
        hit_times = detect_hits(audio) if audio.exists() else np.array([])

    width_px = court_px_width(corners_px)
    player_swings = {"subject": _player_swings(tracks, subject, width_px)}
    if partner is not None:
        player_swings["partner"] = _player_swings(tracks, partner, width_px)
    for i, opp_id in enumerate(opponents[:2]):
        player_swings[f"opponent{i + 1}"] = _player_swings(tracks, opp_id, width_px)

    # drop audio "hits" with no matching wrist swing (warmup/pre-play noise)
    # before segmenting into rallies, so getting-ready footage isn't scored
    hit_times = corroborate_hits(hit_times, player_swings)
    rallies = segment_rallies(hit_times)
    hitters = attribute_hitters(hit_times, player_swings)
    subject_hits = np.array([h == "subject" for h in hitters])
    team_hits = np.array([h in MY_TEAM for h in hitters])

    ev = rally_metrics(rallies, hit_times, subject_hits, duration)
    ev["swing_count"] = int(len(player_swings["subject"]))
    ev["partner_track_id"] = partner
    ev["partner_swing_count"] = int(len(player_swings.get("partner", [])))
    ev["opponent_track_ids"] = opponents
    ev["opponent_track_id"] = opponents[0] if opponents else None  # back-compat scalar
    ev["opponent_swing_count"] = int(len(player_swings.get("opponent1", [])))
    atomic_write_json(sdir / "events.json", ev)

    clips = sdir / "clips"
    clips.mkdir(exist_ok=True)
    for i, r in enumerate(rallies):
        out = clips / f"rally_{i:02d}.mp4"
        if out.exists():
            continue
        start = max(0.0, r["start"] - 1.5)
        dur = min(duration, r["end"] + 1.5) - start
        # stream copy: keyframe-aligned (may start slightly early — fine, it's padding)
        media._run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(sdir / "video.mp4"),
              "-t", f"{dur:.2f}", "-c", "copy", "-an", str(out)])
    return hit_times, team_hits, rallies, hitters


def shots_stage(sdir: Path, calib: CourtCalibration,
                pos: pd.DataFrame, hit_times, subject_hits, rallies,
                corners_px: list[list[float]],
                ball: pd.DataFrame, ball_stats: dict,
                hitters: list[str] | None = None,
                ws: pd.DataFrame | None = None) -> pd.DataFrame:
    """M3: bounces, shot classification, serve depth (ball track prebuilt).

    Returns bounces_court (court-feet coords) so the points stage can reuse
    it for in/out calls without recomputing bounce detection.
    """
    from ball import detect_bounces
    from shots import shot_report

    bounces = detect_bounces(ball, FPS)
    if len(bounces):
        court_xy = calib.to_court(bounces[["x", "y"]].to_numpy())
        bounces_court = pd.DataFrame(
            {"x": court_xy[:, 0], "y": court_xy[:, 1], "t": bounces["t"]})
    else:
        bounces_court = pd.DataFrame(columns=["x", "y", "t"])

    report = shot_report(hit_times, subject_hits, ball, ball_stats, pos,
                         rallies, corners_px, bounces_court, FPS, hitters=hitters, ws=ws)
    # pixel-space bounce points (pre-projection) for the results-screen
    # trajectory overlay — capped so the JSON payload stays small
    BOUNCE_OVERLAY_CAP = 150
    if len(bounces):
        report["bounces_px"] = (bounces[["frame", "x", "y", "t"]]
                                .head(BOUNCE_OVERLAY_CAP).round(1).to_dict("records"))
    else:
        report["bounces_px"] = []
    atomic_write_json(sdir / "shots.json", report)
    return bounces_court


def points_stage(sdir: Path, rallies: list[dict], hit_times, hitters: list[str],
                 bounces_court: pd.DataFrame, ball_available: bool,
                 shots_report: dict) -> None:
    """Rally winners + serve/return win rates from hitter attribution, plus
    serve-placement and shot-type breakdowns joined against shots_report --
    two views the app already computes both halves of but never
    cross-references (see points.serve_side_win_rates/shot_type_outcomes)."""
    outcomes = rally_outcomes(rallies, hit_times, hitters,
                              bounces_court if ball_available else None)
    summary = point_summary(outcomes, ball_available, hitters=hitters)
    summary["serve_side_win_rates"] = serve_side_win_rates(
        shots_report.get("serves") or [], outcomes)
    summary["shot_type_outcomes"] = shot_type_outcomes(
        shots_report.get("shots") or [], rallies, outcomes)
    atomic_write_json(sdir / "points.json", {"outcomes": outcomes, **summary})


def ensure_cuts(sdir: Path, video: Path, stride: int) -> int:
    """Camera-cut count for confidence scoring (metrics.py docks confidence
    above CutDetector.CORR_THRESHOLD-triggered cuts). cuts.json is written
    once during a session's first (fresh-tracking) analysis, for free, as a
    byproduct of that decode -- and it's calibration-independent, so cached-
    track recalibrations correctly never need to redo it. But a session
    whose cuts.json is missing (predates cut detection, or an interrupted
    first run) would otherwise read back a silent, permanent default of 0
    every time (cached-track analysis never re-derives it). Self-heal that
    one case with a standalone decode; every other call is a free file read.
    """
    cuts_f = sdir / "cuts.json"
    cuts = load_json(cuts_f)
    if "cut_frames" not in cuts:  # missing, or corrupt -> regenerate, never a silent 0
        from ball import detect_cuts
        cuts = {"cut_frames": detect_cuts(video, stride=stride)}
        atomic_write_json(cuts_f, cuts)
    return len(cuts["cut_frames"])


def analyze(sdir: Path) -> None:
    """Track players, project subject positions, compute metrics.

    Speed design: ONE video decode — ball candidates are detected inside the
    tracking loop (frame_cb); audio hit detection runs concurrently in its own
    thread; tracks/ball parquets are reused on recalibration.
    """
    import time

    from ball import build_ball_track, run_ball_tracking
    from tracking import TRACK_STRIDE

    try:
        calib_data = load_json(sdir / "calibration.json")
        if not calib_data.get("corners_px") or not calib_data.get("self_px"):
            raise RuntimeError("calibration.json is missing or unreadable — recalibrate this session")
        calib = CourtCalibration(calib_data["corners_px"], calib_data.get("kitchen_px"))
        info = media.probe_video(sdir / "video.mp4")
        total_frames = max(1, info["frames"])

        # audio hit detection is independent of tracking — run concurrently
        audio_result: dict = {}

        def _audio_job():
            audio = sdir / "audio.wav"
            audio_result["hits"] = detect_hits(audio) if audio.exists() else np.array([])

        audio_thread = threading.Thread(target=_audio_job, daemon=True)
        audio_thread.start()

        set_status(sdir, "tracking", "running", progress=0.0)
        tracks_pq = sdir / "tracks.parquet"
        ball_pq = sdir / "ball.parquet"
        if tracks_pq.exists():
            tracks = pd.read_parquet(tracks_pq)  # recalibration: reuse detection
            if ball_pq.exists():
                ball = pd.read_parquet(ball_pq)
                sampled = max(1, total_frames // TRACK_STRIDE)
                ball_stats = {"coverage": round(float(ball["frame"].nunique()) / sampled, 3)
                              if len(ball) else 0.0,
                              "segments": int(ball["seg"].nunique()) if len(ball) else 0,
                              "frames": total_frames, "stride": TRACK_STRIDE}
            else:
                ball, ball_stats = run_ball_tracking(sdir / "video.mp4", tracks,
                                                     ball_pq, FPS)
        else:
            from ball import BallDetector, CutDetector
            balldet = BallDetector()
            cutdet = CutDetector()
            t0 = time.monotonic()

            def _progress(real_f: int) -> None:
                frac = min(1.0, real_f / total_frames)
                elapsed = time.monotonic() - t0
                eta = (elapsed / frac - elapsed) / 0.80 if frac > 0.02 else None
                set_status(sdir, "tracking", "running", progress=frac,
                           eta_seconds=eta)

            def _frame_cb(f, img, boxes):
                balldet.update(f, img, boxes)
                cutdet.update(f, img)

            tracks = run_tracking(sdir / "video.mp4", tracks_pq, MODELS,
                                  progress_cb=_progress, frame_cb=_frame_cb)
            ball, ball_stats = build_ball_track(
                balldet.candidates, total_frames, ball_pq, stride=TRACK_STRIDE)
            atomic_write_json(sdir / "cuts.json", {"cut_frames": cutdet.cut_frames})

        set_status(sdir, "metrics", "running")
        meta = load_json(sdir / "meta.json")
        match_type = meta.get("match_type", "singles")
        doubles = match_type == "doubles"

        subject = pick_subject(tracks, tuple(calib_data["self_px"]))
        pos = subject_court_positions(tracks, subject, calib, FPS)
        pos.to_parquet(sdir / "positions.parquet", index=False)

        subject_ids = stitch_chain_ids(tracks, subject) if len(tracks) else {subject}
        partner = None
        partner_ids: set[int] = set()
        if doubles and calib_data.get("partner_px") and len(tracks):
            partner = pick_subject(tracks, tuple(calib_data["partner_px"]))
            partner_ids = stitch_chain_ids(tracks, partner)

        subject_median_y = float(np.median(pos["y"])) if len(pos) else NET_Y
        exclude_ids = subject_ids | partner_ids
        opponents = pick_opponents(tracks, calib, exclude_ids, subject_median_y,
                                   n=2 if doubles else 1) if len(tracks) else []

        # 4 known players is expected in doubles, not an ambiguity signal —
        # only dock confidence for an unexpected extra person in singles.
        # Opponents need the same stitching subject/partner already get:
        # the tracker fragments ids on occlusion/camera motion, so without
        # this every re-acquired fragment of the SAME opponent looked like
        # a brand-new "extra person" on court (observed: 178 phantom
        # players from one real opponent in a single-camera clip).
        opponent_ids: set[int] = set()
        for opp_id in opponents:
            opponent_ids |= stitch_chain_ids(tracks, opp_id)

        secondary_court_tracks = 0
        if not doubles and len(tracks):
            all_ids = exclude_ids | opponent_ids
            secondary_court_tracks = count_secondary_court_tracks(tracks, calib, all_ids)

        n_cuts = ensure_cuts(sdir, sdir / "video.mp4", TRACK_STRIDE)
        m = compute_metrics(pos, FPS, camera_cuts=n_cuts,
                            secondary_court_tracks=secondary_court_tracks)
        m["match_type"] = match_type
        m["subject_track_id"] = subject
        m["partner_track_id"] = partner
        m["opponent_track_ids"] = opponents
        m["opponent_track_id"] = opponents[0] if opponents else None  # back-compat scalar
        atomic_write_json(sdir / "metrics.json", m)

        set_status(sdir, "events", "running")
        audio_thread.join(timeout=120)
        hit_times, team_hits, rallies, hitters = events(
            sdir, tracks, subject, partner, opponents, calib_data["corners_px"],
            hit_times=audio_result.get("hits"), video_duration=info["duration"])

        set_status(sdir, "shots", "running")
        subject_chain = stitch_subject(tracks, subject) if len(tracks) else pd.DataFrame()
        ws_subject = (wrist_speed(subject_chain, FPS, court_px_width(calib_data["corners_px"]))
                     if len(subject_chain) else pd.DataFrame(columns=["t", "speed"]))
        bounces_court = shots_stage(sdir, calib, pos, hit_times, team_hits, rallies,
                                    calib_data["corners_px"], ball, ball_stats,
                                    hitters=hitters, ws=ws_subject)

        set_status(sdir, "points", "running")
        shots_report = load_json(sdir / "shots.json")
        points_stage(sdir, rallies, hit_times, hitters, bounces_court,
                    ball_available=bool(shots_report.get("available")),
                    shots_report=shots_report)

        set_status(sdir, "rating", "running")
        from dupr import estimate
        from feedback import coach_tips, drills_for_weak, fatigue_note
        m_final = load_json(sdir / "metrics.json")
        rating = estimate(
            m_final,
            load_json(sdir / "events.json"),
            load_json(sdir / "shots.json"),
            calibration=calib_data,
        )
        rating["tips"] = coach_tips(rating)
        note = fatigue_note(m_final.get("movement_curve") or [])
        if note:
            rating["tips"].append(note)
        rating["drills"] = drills_for_weak(rating)
        atomic_write_json(sdir / "dupr.json", rating)
        set_status(sdir, "done", "done")
        notify.notify_webhook(sdir, ok=True)
    except Exception as e:
        log.exception("analysis failed for %s", sdir.name)
        set_status(sdir, get_status(sdir).get("stage", "analyze"), "error", str(e))
        notify.notify_webhook(sdir, ok=False, error=str(e))
