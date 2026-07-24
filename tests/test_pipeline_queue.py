"""Analysis queue position tracking + friendly ingest error mapping."""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd

import pipeline

TRACK_COLUMNS = ["frame", "track_id", "x1", "y1", "x2", "y2", "conf", "lwx", "lwy", "rwx", "rwy"]


def test_events_uses_real_video_duration_not_tracked_frames(tmp_path, monkeypatch):
    """play_time_pct used to divide by the last TRACKED frame's timestamp,
    which under-reports true length whenever players go undetected near the
    end of a clip while a rally is still audible -- and could push the
    percentage above 100%. It should use the real (ffprobe) video duration
    instead, which analyze() already has on hand."""
    monkeypatch.setattr(pipeline, "_run", lambda cmd: None)  # skip real ffmpeg clip cuts
    tracks = pd.DataFrame(columns=TRACK_COLUMNS)  # empty -> old tracked-frame duration was 0.0
    hit_times = np.array([1.0, 1.3, 1.6])  # one rally of 3 hits, 0.6s long
    corners_px = [[300.0, 150.0], [1000.0, 150.0], [1250.0, 700.0], [50.0, 700.0]]
    pipeline.events(tmp_path, tracks, subject=1, partner=None, opponents=[], corners_px=corners_px,
                    hit_times=hit_times, video_duration=100.0)
    ev = json.loads((tmp_path / "events.json").read_text())
    assert ev["rally_count"] == 1
    # old code: video_duration falsy fallback (0.0) -> play_time_pct forced to 0.0 always
    assert ev["play_time_pct"] == 0.6


def test_ensure_cuts_self_heals_when_missing(tmp_path, monkeypatch):
    """A session whose cuts.json never got written (legacy session, or an
    interrupted first analysis) must not silently read back a permanent
    camera_cuts=0 -- ensure_cuts should recompute it from the video."""
    calls = []

    def fake_detect_cuts(video, stride):
        calls.append((video, stride))
        return [5, 40]

    monkeypatch.setattr("ball.detect_cuts", fake_detect_cuts)
    n = pipeline.ensure_cuts(tmp_path, tmp_path / "video.mp4", stride=2)
    assert n == 2
    assert calls == [(tmp_path / "video.mp4", 2)]
    assert json.loads((tmp_path / "cuts.json").read_text())["cut_frames"] == [5, 40]


def test_ensure_cuts_reuses_existing_without_redecoding(tmp_path, monkeypatch):
    """cuts.json is calibration-independent -- once written it must never
    trigger a second (wasted) video decode on a later cached-track reprocess."""
    (tmp_path / "cuts.json").write_text(json.dumps({"cut_frames": [1, 2, 3]}))

    def boom(*a, **kw):
        raise AssertionError("should not redecode when cuts.json already exists")

    monkeypatch.setattr("ball.detect_cuts", boom)
    n = pipeline.ensure_cuts(tmp_path, tmp_path / "video.mp4", stride=2)
    assert n == 3


def test_queue_position_reflects_order(monkeypatch, tmp_path):
    release = threading.Event()

    def fake_analyze(sdir):
        release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "analyze", fake_analyze)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    try:
        pipeline.enqueue_analyze(a)
        pipeline.enqueue_analyze(b)
        time.sleep(0.1)  # let the worker pick up `a`
        assert pipeline.queue_position(a) == 0   # running
        assert pipeline.queue_position(b) == 1   # waiting behind a
    finally:
        release.set()
        pipeline._jobs.join()


def test_queue_position_none_when_not_queued(tmp_path):
    assert pipeline.queue_position(tmp_path / "never_queued") is None


def test_friendly_error_maps_known_ffmpeg_failures():
    assert "corrupted" in pipeline._friendly_error("Invalid data found when processing input")
    assert "readable video track" in pipeline._friendly_error("no video stream found")
    assert "truncated" in pipeline._friendly_error("moov atom not found")
    assert pipeline._friendly_error("some unrelated error") == "some unrelated error"


def test_notify_webhook_noop_without_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DINKIQ_WEBHOOK_URL", raising=False)
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
    pipeline.notify_webhook(tmp_path, ok=True)
    assert calls == []


def test_notify_webhook_posts_when_url_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    (tmp_path / "meta.json").write_text('{"label": "Test Session"}')
    calls = []
    monkeypatch.setattr("requests.post", lambda url, json, timeout: calls.append((url, json, timeout)))
    pipeline.notify_webhook(tmp_path, ok=True)
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://example.invalid/hook"
    assert "Test Session" in payload["content"]
    assert "finished" in payload["content"]


def test_notify_webhook_error_message_included(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    calls = []
    monkeypatch.setattr("requests.post", lambda url, json, timeout: calls.append(json))
    pipeline.notify_webhook(tmp_path, ok=False, error="ball coverage too low")
    assert "ball coverage too low" in calls[0]["content"]


def test_notify_webhook_never_raises_on_post_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    def boom(*a, **kw): raise ConnectionError("no network")
    monkeypatch.setattr("requests.post", boom)
    pipeline.notify_webhook(tmp_path, ok=True)  # must not raise


if __name__ == "__main__":
    print("run via pytest (uses monkeypatch/tmp_path fixtures)")
