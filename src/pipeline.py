"""Session pipeline orchestration.

Each stage writes a durable artifact under data/sessions/<id>/ so any stage is
resumable:
  raw.<ext>          original upload
  video.mp4          normalized 720p/30fps
  audio.wav          extracted audio (for M2 hit detection)
  frame0.jpg         first frame (calibration UI)
  calibration.json   court corners + subject click (written by server)
  tracks.parquet     all person tracks
  positions.parquet  subject court positions
  metrics.json       M1 metrics
  status.json        {stage, state, error?}
"""

import json
import queue
import subprocess
import threading
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from court import NET_Y, CourtCalibration
from events import (attribute_hits, detect_hits, detect_swings, rally_metrics,
                    segment_rallies, wrist_speed)
from metrics import compute_metrics
from points import attribute_hitters, point_summary, rally_outcomes
from tracking import (pick_opponent, pick_subject, run_tracking, stitch_chain_ids,
                      stitch_subject, subject_court_positions)

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "data" / "sessions"
MODELS = ROOT / "models"
FPS = 30.0


def session_dir(sid: str) -> Path:
    d = (SESSIONS / sid).resolve()
    if d.parent != SESSIONS.resolve():  # path traversal guard
        raise ValueError("bad session id")
    return d


# rough share of analysis wall-clock per stage (tracking dominates; measured
# on the benchmark clip) — used for the overall progress bar + ETA
STAGE_WEIGHTS = {"queued": 0.0, "tracking": 0.80, "metrics": 0.82,
                 "events": 0.90, "shots": 0.97, "points": 0.99,
                 "rating": 1.0, "done": 1.0}


def set_status(sdir: Path, stage: str, state: str, error: str | None = None,
               progress: float | None = None,
               eta_seconds: float | None = None) -> None:
    payload: dict = {"stage": stage, "state": state}
    if error:
        payload["error"] = error
    prev_w = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            payload["overall"] = round(prev_w + (w - prev_w) * (progress or 0.0), 3) \
                if progress is not None else round(prev_w, 3)
            break
        prev_w = w
    else:
        payload["overall"] = 1.0 if state == "done" and stage == "done" else 0.0
    if progress is not None:
        payload["progress"] = round(progress, 3)
    if eta_seconds is not None:
        payload["eta_seconds"] = int(eta_seconds)
    (sdir / "status.json").write_text(json.dumps(payload))


def get_status(sdir: Path) -> dict:
    f = sdir / "status.json"
    return json.loads(f.read_text()) if f.exists() else {"stage": "new", "state": "idle"}


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr[-800:]}")


# artifacts derived from calibration/analysis; tracks.parquet and ball.parquet
# are pixel-space (calibration-independent) and deliberately survive so
# recalibration takes seconds instead of re-running detection
DERIVED = ["positions.parquet", "metrics.json", "events.json", "shots.json",
           "points.json", "dupr.json"]


def clear_derived(sdir: Path) -> None:
    for name in DERIVED:
        (sdir / name).unlink(missing_ok=True)
    clips = sdir / "clips"
    if clips.exists():
        for f in clips.iterdir():
            f.unlink()


# ---- single-worker analysis queue: one YOLO/MPS job at a time ----
_jobs: queue.Queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _worker() -> None:
    while True:
        sdir = _jobs.get()
        try:
            analyze(sdir)
        except Exception:
            traceback.print_exc()
        finally:
            _jobs.task_done()


def enqueue_analyze(sdir: Path) -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True).start()
            _worker_started = True
    set_status(sdir, "queued", "queued")
    _jobs.put(sdir)


def _has_audio(raw: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(raw)],
        capture_output=True, text=True)
    return bool(proc.stdout.strip())


def probe_video(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True)
    info = json.loads(proc.stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    duration = float(info.get("format", {}).get("duration") or 0.0)
    frames = int(st.get("nb_frames") or 0) or int(duration * fps)
    return {"codec": st.get("codec_name"), "width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0), "fps": round(fps, 3),
            "frames": frames, "duration": round(duration, 2)}


def ingest(sdir: Path, raw: Path) -> None:
    """Normalize video, extract audio (if present) + first frame.

    Fast path: already-H.264, <=720p, ~30fps input is remuxed (-c copy)
    instead of re-encoded — near-instant for typical phone exports.
    """
    try:
        set_status(sdir, "ingest", "running")
        video = sdir / "video.mp4"
        src = probe_video(raw)
        if (src["codec"] == "h264" and src["height"] <= 720
                and 29.0 <= src["fps"] <= 31.0):
            _run(["ffmpeg", "-y", "-i", str(raw), "-an", "-c:v", "copy", str(video)])
        else:
            _run(["ffmpeg", "-y", "-i", str(raw),
                  "-vf", "scale=-2:720", "-r", str(int(FPS)),
                  "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", str(video)])
        if _has_audio(raw):  # audio only needed for hit detection
            _run(["ffmpeg", "-y", "-i", str(raw), "-vn", "-ac", "1", "-ar", "22050",
                  str(sdir / "audio.wav")])
        _run(["ffmpeg", "-y", "-i", str(video), "-vframes", "1", "-q:v", "3",
              str(sdir / "frame0.jpg")])
        (sdir / "ingest.json").write_text(json.dumps(probe_video(video)))
        set_status(sdir, "ingest", "done")
    except Exception as e:
        traceback.print_exc()
        set_status(sdir, "ingest", "error", str(e))


def events(sdir: Path, tracks: pd.DataFrame, subject: int, opponent: int | None,
           hit_times: np.ndarray | None = None):
    """M2: paddle hits from audio, rallies, swings, per-rally clips.

    Degrades gracefully: no/quiet audio -> events.json records zero rallies.
    hit_times may be precomputed (analyze runs audio concurrently with tracking).
    Returns (hit_times, subject_hits, rallies, hitters) — hitters is a per-hit
    'subject'/'opponent'/'unknown' label for the points stage.
    """
    audio = sdir / "audio.wav"
    duration = float(tracks["frame"].max() + 1) / FPS if len(tracks) else 0.0
    if hit_times is None:
        hit_times = detect_hits(audio) if audio.exists() else np.array([])
    rallies = segment_rallies(hit_times)

    sub = stitch_subject(tracks, subject) if len(tracks) else tracks
    subject_swings = detect_swings(wrist_speed(sub, FPS)) if len(sub) else np.array([])
    subject_hits = attribute_hits(hit_times, subject_swings)

    if opponent is not None and len(tracks):
        opp = stitch_subject(tracks, opponent)
        opponent_swings = detect_swings(wrist_speed(opp, FPS)) if len(opp) else np.array([])
    else:
        opponent_swings = np.array([])
    hitters = attribute_hitters(hit_times, subject_swings, opponent_swings)

    ev = rally_metrics(rallies, hit_times, subject_hits, duration)
    ev["swing_count"] = int(len(subject_swings))
    ev["opponent_track_id"] = opponent
    ev["opponent_swing_count"] = int(len(opponent_swings))
    (sdir / "events.json").write_text(json.dumps(ev))

    clips = sdir / "clips"
    clips.mkdir(exist_ok=True)
    for i, r in enumerate(rallies):
        out = clips / f"rally_{i:02d}.mp4"
        if out.exists():
            continue
        start = max(0.0, r["start"] - 1.5)
        dur = min(duration, r["end"] + 1.5) - start
        # stream copy: keyframe-aligned (may start slightly early — fine, it's padding)
        _run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(sdir / "video.mp4"),
              "-t", f"{dur:.2f}", "-c", "copy", "-an", str(out)])
    return hit_times, subject_hits, rallies, hitters


def shots_stage(sdir: Path, calib: CourtCalibration,
                pos: pd.DataFrame, hit_times, subject_hits, rallies,
                corners_px: list[list[float]],
                ball: pd.DataFrame, ball_stats: dict) -> pd.DataFrame:
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
                         rallies, corners_px, bounces_court, FPS)
    (sdir / "shots.json").write_text(json.dumps(report))
    return bounces_court


def points_stage(sdir: Path, rallies: list[dict], hit_times, hitters: list[str],
                 bounces_court: pd.DataFrame, ball_available: bool) -> None:
    """Rally winners + serve/return win rates from hitter attribution."""
    outcomes = rally_outcomes(rallies, hit_times, hitters,
                              bounces_court if ball_available else None)
    summary = point_summary(outcomes, ball_available)
    (sdir / "points.json").write_text(json.dumps({"outcomes": outcomes, **summary}))


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
        calib_data = json.loads((sdir / "calibration.json").read_text())
        calib = CourtCalibration(calib_data["corners_px"], calib_data.get("kitchen_px"))
        info = probe_video(sdir / "video.mp4")
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
            (sdir / "cuts.json").write_text(json.dumps(
                {"cut_frames": cutdet.cut_frames}))

        set_status(sdir, "metrics", "running")
        subject = pick_subject(tracks, tuple(calib_data["self_px"]))
        pos = subject_court_positions(tracks, subject, calib, FPS)
        pos.to_parquet(sdir / "positions.parquet", index=False)

        subject_ids = stitch_chain_ids(tracks, subject) if len(tracks) else {subject}
        subject_median_y = float(np.median(pos["y"])) if len(pos) else NET_Y
        opponent = pick_opponent(tracks, calib, subject_ids, subject_median_y) \
            if len(tracks) else None

        cuts_f = sdir / "cuts.json"
        n_cuts = len(json.loads(cuts_f.read_text())["cut_frames"]) if cuts_f.exists() else 0
        m = compute_metrics(pos, FPS, camera_cuts=n_cuts)
        m["subject_track_id"] = subject
        m["opponent_track_id"] = opponent
        (sdir / "metrics.json").write_text(json.dumps(m))

        set_status(sdir, "events", "running")
        audio_thread.join(timeout=120)
        hit_times, subject_hits, rallies, hitters = events(
            sdir, tracks, subject, opponent, hit_times=audio_result.get("hits"))

        set_status(sdir, "shots", "running")
        bounces_court = shots_stage(sdir, calib, pos, hit_times, subject_hits, rallies,
                                    calib_data["corners_px"], ball, ball_stats)

        set_status(sdir, "points", "running")
        shots_report = json.loads((sdir / "shots.json").read_text())
        points_stage(sdir, rallies, hit_times, hitters, bounces_court,
                    ball_available=bool(shots_report.get("available")))

        set_status(sdir, "rating", "running")
        from dupr import estimate
        from feedback import coach_tips
        rating = estimate(
            json.loads((sdir / "metrics.json").read_text()),
            json.loads((sdir / "events.json").read_text()),
            json.loads((sdir / "shots.json").read_text()),
            calibration=calib_data,
        )
        rating["tips"] = coach_tips(rating)
        (sdir / "dupr.json").write_text(json.dumps(rating))
        set_status(sdir, "done", "done")
    except Exception as e:
        traceback.print_exc()
        set_status(sdir, get_status(sdir).get("stage", "analyze"), "error", str(e))
