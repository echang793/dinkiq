"""Pose-model device/size selection and ingest normalization.

Measured on the sample (Apple Silicon): ultralytics runs on the CPU unless asked
(32 ms/frame at 640); MPS is 10.6 ms at 640 and 16.8 ms at 960, which also sees
small far-court players better. Ingest used to keep sub-720p sources at native
size (every pixel threshold downstream assumes 720p), kept a portrait video's
rotation as side-data (cv2 may ignore it), and let x264 pick an 8-second GOP.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import media
import tracking
from api import uploads


# ── device / size ───────────────────────────────────────────────────────

def test_select_device_prefers_mps_then_cpu(monkeypatch):
    import torch
    monkeypatch.delenv("DINKIQ_DEVICE", raising=False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    assert tracking.select_device() == "mps"
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert tracking.select_device() == "cpu"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert tracking.select_device() == "cuda:0"


def test_device_can_be_forced_by_env(monkeypatch):
    monkeypatch.setenv("DINKIQ_DEVICE", "cpu")
    assert tracking.select_device() == "cpu"


def test_pose_imgsz_default_and_override(monkeypatch):
    monkeypatch.delenv("DINKIQ_POSE_IMGSZ", raising=False)
    assert tracking.pose_imgsz() == 960
    monkeypatch.setenv("DINKIQ_POSE_IMGSZ", "640")
    assert tracking.pose_imgsz() == 640
    monkeypatch.setenv("DINKIQ_POSE_IMGSZ", "banana")
    assert tracking.pose_imgsz() == 960


class _FakeResult:
    boxes = None
    keypoints = None
    orig_img = np.zeros((4, 4, 3), dtype=np.uint8)


def _fake_yolo(fail_on=()):
    calls = []

    class FakeYOLO:
        def __init__(self, path):
            self.path = path

        def track(self, **kw):
            calls.append(kw)
            if kw.get("device") in fail_on:
                def boom():
                    raise RuntimeError("MPS backend: op not implemented")
                    yield  # pragma: no cover
                return boom()
            return iter([_FakeResult(), _FakeResult(), _FakeResult()])
    return FakeYOLO, calls


def test_run_tracking_passes_device_and_imgsz_and_records_them(monkeypatch, tmp_path):
    Fake, calls = _fake_yolo()
    monkeypatch.setattr(tracking, "YOLO", Fake)
    monkeypatch.setattr(tracking, "select_device", lambda: "mps")
    monkeypatch.delenv("DINKIQ_POSE_IMGSZ", raising=False)
    tracking.run_tracking(tmp_path / "v.mp4", tmp_path / "tracks.parquet", tmp_path)
    assert calls[0]["device"] == "mps" and calls[0]["imgsz"] == 960
    meta = json.loads((tmp_path / "tracks.meta.json").read_text())
    assert meta == {"model": tracking.MODEL_NAME, "imgsz": 960, "device": "mps", "stride": tracking.TRACK_STRIDE}


def test_run_tracking_falls_back_to_cpu_if_mps_fails_before_any_frame(monkeypatch, tmp_path):
    Fake, calls = _fake_yolo(fail_on=("mps",))
    monkeypatch.setattr(tracking, "YOLO", Fake)
    monkeypatch.setattr(tracking, "select_device", lambda: "mps")
    df = tracking.run_tracking(tmp_path / "v.mp4", tmp_path / "tracks.parquet", tmp_path)
    assert [c["device"] for c in calls] == ["mps", "cpu"]
    assert list(df.columns) == tracking.COLUMNS
    assert json.loads((tmp_path / "tracks.meta.json").read_text())["device"] == "cpu"


def test_a_cpu_failure_is_not_swallowed(monkeypatch, tmp_path):
    Fake, _ = _fake_yolo(fail_on=("cpu",))
    monkeypatch.setattr(tracking, "YOLO", Fake)
    monkeypatch.setattr(tracking, "select_device", lambda: "cpu")
    with pytest.raises(RuntimeError):
        tracking.run_tracking(tmp_path / "v.mp4", tmp_path / "tracks.parquet", tmp_path)


# ── cached tracks and model changes ─────────────────────────────────────

def test_tracks_made_with_a_different_model_or_size_are_stale(monkeypatch):
    monkeypatch.delenv("DINKIQ_POSE_IMGSZ", raising=False)
    cur = {"model": tracking.MODEL_NAME, "imgsz": 960, "device": "mps", "stride": 2}
    assert tracking.tracks_are_stale(cur) is False
    assert tracking.tracks_are_stale({**cur, "imgsz": 640}) is True
    assert tracking.tracks_are_stale({**cur, "model": "yolov8n-pose.pt"}) is True
    assert tracking.tracks_are_stale({**cur, "device": "cpu"}) is False   # same results either way


def test_legacy_tracks_without_metadata_are_trusted():
    """Sessions analysed before this existed: don't silently redo minutes of work."""
    assert tracking.tracks_are_stale(None) is False
    assert tracking.tracks_are_stale({}) is False


# ── ingest normalization ────────────────────────────────────────────────

def _info(**kw):
    base = {"codec": "h264", "width": 1280, "height": 720, "fps": 30.0, "rotation": 0,
            "vfr": False, "duration": 60.0}
    return {**base, **kw}


def test_a_clean_720p30_h264_is_remuxed():
    assert media.normalize_plan(_info())["mode"] == "copy"


@pytest.mark.parametrize("override,why", [
    ({"height": 360, "width": 640}, "upscale"),          # below 720p: thresholds assume 720p
    ({"height": 1080, "width": 1920}, "downscale"),
    ({"codec": "hevc"}, "codec"),
    ({"fps": 60.0}, "fps"),
    ({"fps": 29.97}, "fps"),                              # drifts ~4 s over a 1 h match vs audio
    ({"vfr": True}, "variable frame rate"),
    ({"rotation": 90}, "rotation"),
    ({"rotation": 270, "width": 720, "height": 1280}, "rotation"),
])
def test_everything_else_is_reencoded_with_a_reason(override, why):
    plan = media.normalize_plan(_info(**override))
    assert plan["mode"] == "reencode" and why in plan["reason"]


def test_reencode_command_is_cfr_720p_with_short_gop_and_autorotate():
    cmd = media.normalize_command(Path("in.mov"), Path("out.mp4"), _info(rotation=90))
    j = " ".join(cmd)
    assert "scale=-2:720" in j and "-fps_mode cfr" in j and "-r 30" in j
    assert "-g 30" in j and "libx264" in j and "-an" in j
    assert "-noautorotate" not in j  # ffmpeg applies the rotation when re-encoding


def test_copy_command_is_a_plain_remux():
    cmd = media.normalize_command(Path("in.mp4"), Path("out.mp4"), _info())
    assert "copy" in cmd and "libx264" not in cmd


def _fake_ffprobe(monkeypatch, stream, duration="12.5"):
    payload = json.dumps({"streams": [stream], "format": {"duration": duration}})
    monkeypatch.setattr(media.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=payload, stderr=""))


def test_probe_reads_rotation_from_side_data_and_tags(monkeypatch, tmp_path):
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1",
                                "avg_frame_rate": "30/1", "side_data_list": [{"rotation": -90}]})
    assert media.probe_video(tmp_path / "x.mov")["rotation"] == 90   # normalized to 0/90/180/270
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1",
                                "avg_frame_rate": "30/1", "tags": {"rotate": "270"}})
    assert media.probe_video(tmp_path / "x.mov")["rotation"] == 270


def test_probe_flags_variable_frame_rate(monkeypatch, tmp_path):
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1280, "height": 720,
                                "r_frame_rate": "30/1", "avg_frame_rate": "2400/100"})
    info = media.probe_video(tmp_path / "x.mp4")
    assert info["vfr"] is True and info["fps"] == 30.0
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1280, "height": 720,
                                "r_frame_rate": "30/1", "avg_frame_rate": "30/1"})
    assert media.probe_video(tmp_path / "x.mp4")["vfr"] is False


def test_probe_reports_display_orientation(monkeypatch, tmp_path):
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1",
                                "avg_frame_rate": "30/1", "side_data_list": [{"rotation": 90}]})
    info = media.probe_video(tmp_path / "x.mov")
    assert info["portrait"] is True            # a landscape sensor rotated 90 degrees displays portrait
    _fake_ffprobe(monkeypatch, {"codec_name": "h264", "width": 1920, "height": 1080,
                                "r_frame_rate": "30/1", "avg_frame_rate": "30/1"})
    assert media.probe_video(tmp_path / "x.mov")["portrait"] is False


def test_portrait_capture_gets_a_filming_hint():
    hint = uploads.capture_hint({"width": 1080, "height": 1920, "fps": 30.0, "portrait": True})
    assert hint and "landscape" in hint.lower()
    assert uploads.capture_hint({"width": 1280, "height": 720, "fps": 30.0, "portrait": False}) is None


# ── analyze discards cached tracks from another model, keeps everything else ──

def _cached(tmp_path, meta):
    for name in ("tracks.parquet", "ball.parquet", "cuts.json"):
        (tmp_path / name).write_bytes(b"x")
    if meta is not None:
        (tmp_path / "tracks.meta.json").write_text(json.dumps(meta))
    return tmp_path


def test_stale_cached_tracks_are_discarded_so_detection_reruns(tmp_path, monkeypatch):
    import stages
    monkeypatch.delenv("DINKIQ_POSE_IMGSZ", raising=False)
    d = _cached(tmp_path, {"model": tracking.MODEL_NAME, "imgsz": 640, "device": "cpu", "stride": 2})
    assert stages.discard_stale_tracks(d) is True
    assert not any((d / n).exists() for n in ("tracks.parquet", "ball.parquet", "tracks.meta.json", "cuts.json"))


def test_current_and_legacy_cached_tracks_are_kept(tmp_path, monkeypatch):
    import stages
    monkeypatch.delenv("DINKIQ_POSE_IMGSZ", raising=False)
    for label, meta in (("current", {"model": tracking.MODEL_NAME, "imgsz": 960, "device": "mps", "stride": 2}),
                        ("legacy", None)):
        sub = tmp_path / label
        sub.mkdir()
        _cached(sub, meta)
        assert stages.discard_stale_tracks(sub) is False, label
        assert (sub / "tracks.parquet").exists() and (sub / "ball.parquet").exists(), label
