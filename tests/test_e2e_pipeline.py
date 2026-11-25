"""A real clip through the real pipeline: ffmpeg ingest -> pose model -> every stage.

Everything else in the suite exercises pieces with synthetic data or mocks, so a
break BETWEEN stages (a column one stage writes and the next doesn't read, a status
that never reaches done, a NaN that only appears with real keypoints) can pass every
unit test. This runs ~12 s of the sample footage end to end.

It asserts structure and sanity, not accuracy: there are no labels, and one short
broadcast clip can't say the numbers are right -- only that the machinery works and
nothing it writes is malformed.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import media
import pipeline
from court import CourtCalibration, on_court_mask
import session_status
import stages
import tracking

ROOT = Path(__file__).resolve().parent.parent
CLIP = ROOT / "tests" / "fixtures" / "rally_clip_12s.mp4"
WEIGHTS = pipeline.MODELS / tracking.MODEL_NAME

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not WEIGHTS.exists(), reason=f"{WEIGHTS.name} not downloaded (see README)"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

# the sample's court corners (full-frame 1280x720 coordinates) -- the fixture is 640x360
# upscaled by ingest to 1280x720, the same geometry the sample was calibrated in
CORNERS = [[352, 156], [950, 156], [1240, 640], [30, 640]]


def _no_nan(path: Path):
    def refuse(token):
        raise AssertionError(f"{path.name} contains the non-JSON constant {token}")
    return json.loads(path.read_text(), parse_constant=refuse)


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    # NOT under pipeline.SESSIONS: conftest wipes that between every test, and this
    # session is deliberately built once and shared by the whole module
    sdir = tmp_path_factory.mktemp("e2e_session")
    raw = sdir / "raw.mp4"
    shutil.copy(CLIP, raw)
    (sdir / "meta.json").write_text(json.dumps({"filename": "rally_clip_12s.mp4", "match_type": "singles"}))

    media.ingest(sdir, raw)
    assert session_status.get_status(sdir)["state"] == "done", session_status.get_status(sdir)

    # real detection first, so the "tap" can land on a real detected person
    tracks = tracking.run_tracking(sdir / "video.mp4", sdir / "tracks.parquet", pipeline.MODELS)
    assert len(tracks), "the pose model detected nobody in the fixture"
    # tap a real PLAYER: in broadcast footage the first person detected can be a commentator
    # or spectator, who has no on-court positions to analyse (and the pipeline rightly refuses)
    calib = CourtCalibration(CORNERS)
    best = (0, None)
    for tid, g in tracks.groupby("track_id"):
        on_court = int(on_court_mask(calib.to_court(tracking.feet_px(g))).sum())
        if on_court > best[0]:
            best = (on_court, g.sort_values("frame").iloc[0])
    assert best[1] is not None and best[0] >= 90, "no one spent time on the court in the fixture"
    first = best[1]
    self_px = [float((first.x1 + first.x2) / 2), float((first.y1 + first.y2) / 2)]
    (sdir / "calibration.json").write_text(json.dumps({"corners_px": CORNERS, "self_px": self_px}))

    stages.analyze(sdir)
    return sdir


def test_ingest_produced_a_720p_cfr_video_with_audio_and_a_frame(session):
    info = media.probe_video(session / "video.mp4")
    assert (info["height"], info["fps"], info["vfr"]) == (720, 30.0, False)
    assert (session / "audio.wav").stat().st_size > 10_000
    assert (session / "frame0.jpg").stat().st_size > 1_000


def test_detection_recorded_what_made_it(session):
    meta = json.loads((session / "tracks.meta.json").read_text())
    assert meta["model"] == tracking.MODEL_NAME and meta["imgsz"] == tracking.pose_imgsz()
    assert meta["device"] in ("mps", "cpu", "cuda:0")


def test_analysis_reached_done(session):
    st = session_status.get_status(session)
    assert (st["stage"], st["state"]) == ("done", "done"), st


def test_every_stage_wrote_its_artifact_and_none_contain_nan(session):
    for name in ("metrics.json", "events.json", "hits.json", "shots.json", "points.json", "dupr.json", "cuts.json"):
        assert (session / name).exists(), f"{name} missing"
        _no_nan(session / name)
    for name in ("tracks.parquet", "ball.parquet", "positions.parquet"):
        assert (session / name).exists(), f"{name} missing"


def test_the_subject_was_found_and_positions_are_on_the_court(session):
    m = _no_nan(session / "metrics.json")
    assert isinstance(m["subject_track_id"], int)
    assert m["partner_track_id"] is None and m["match_type"] == "singles"
    assert m["frames_analyzed"] > 30 and m["zone_scope"] in ("live", "all")
    assert abs(sum(m["zone_pct"].values()) - 100.0) < 0.5


def test_events_and_hits_agree_with_each_other(session):
    e, hits = _no_nan(session / "events.json"), _no_nan(session / "hits.json")
    in_rallies = [h for h in hits if h["rally"] is not None]
    assert e.get("isolated_hits", 0) == len(hits) - len(in_rallies)
    for h in hits:
        assert h["hitter"] in ("subject", "opponent1", "opponent2", "partner", "unknown")
    if e["rally_count"]:
        assert max(h["rally"] for h in in_rallies) < e["rally_count"]


def test_the_rating_is_never_a_bare_number(session):
    d = _no_nan(session / "dupr.json")
    if d["available"]:
        assert d["band_low"] <= d["band"] <= d["band_high"]
        assert d["confidence_level"] in ("low", "medium", "high") and d["calibrated"] is False
    else:
        assert d["reason"]


def test_ball_metrics_are_either_earned_or_withheld_with_a_reason(session):
    s = _no_nan(session / "shots.json")
    assert s["ball"]["segments"] <= s["ball"].get("raw_segments", s["ball"]["segments"])
    if not s["available"]:
        assert s["reason"]


def test_reanalysing_reuses_the_cached_detection_and_gets_the_same_answer(session):
    before = _no_nan(session / "metrics.json")
    mtime = (session / "tracks.parquet").stat().st_mtime
    pipeline.clear_derived(session)
    stages.analyze(session)
    assert (session / "tracks.parquet").stat().st_mtime == mtime          # detection not redone
    assert _no_nan(session / "metrics.json")["subject_track_id"] == before["subject_track_id"]
    assert session_status.get_status(session)["state"] == "done"


def test_a_tap_on_nobody_fails_with_a_message_instead_of_analysing_someone_else(session):
    cal = json.loads((session / "calibration.json").read_text())
    bad = {**cal, "self_px": [5.0, 5.0]}                      # the top-left corner of the frame
    (session / "calibration.json").write_text(json.dumps(bad))
    try:
        pipeline.clear_derived(session)
        stages.analyze(session)
        st = session_status.get_status(session)
        assert st["state"] == "error" and "tap" in st["error"].lower(), st
    finally:
        (session / "calibration.json").write_text(json.dumps(cal))
