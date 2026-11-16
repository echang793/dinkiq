"""Health endpoint, logging, missing-tool handling, ingest-failure alerts.

The motivating incident: the launchd service crash-looped for ~14 days (119k
identical lines in a 14 MB log with no timestamps) and nothing told anyone --
the only notifier fired from analyze(), and it was switched off anyway.
"""

import json
import logging
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import logsetup
import pipeline
import jobs
import media
import notify
import session_status
import server

client = TestClient(server.app)
Usage = namedtuple("Usage", "total used free")
GB = 1 << 30


@pytest.fixture
def healthy(monkeypatch):
    """Everything the health check looks at, in a good state."""
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: Usage(500 * GB, 100 * GB, 400 * GB))
    monkeypatch.setattr(server, "_weights_present", lambda: True)


# ── /api/health ─────────────────────────────────────────────────────────

def test_health_ok_when_everything_is_fine(healthy):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert {"ffmpeg", "ffprobe", "disk", "data_dir", "weights", "worker"} <= set(body["checks"])
    assert all(c["ok"] for c in body["checks"].values())
    assert body["uptime_s"] >= 0 and "queue" in body


@pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe"])
def test_health_fails_when_a_media_tool_is_missing(healthy, monkeypatch, missing):
    """launchd's minimal PATH once hid ffprobe: the server served fine while
    every analysis died. Health must say so."""
    monkeypatch.setattr(server.shutil, "which", lambda name: None if name == missing else f"/usr/bin/{name}")
    r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["checks"][missing]["ok"] is False
    assert "PATH" in body["checks"][missing]["detail"]


def test_health_fails_when_disk_is_below_the_reserve(healthy, monkeypatch):
    monkeypatch.setattr(server.shutil, "disk_usage", lambda p: Usage(500 * GB, 499 * GB, GB // 2))
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["checks"]["disk"]["ok"] is False


def test_health_fails_when_pose_weights_are_missing(healthy, monkeypatch):
    """The apostrophe-in-path bug: weights not where the loader looks."""
    monkeypatch.setattr(server, "_weights_present", lambda: False)
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["checks"]["weights"]["ok"] is False


def test_health_fails_when_the_data_dir_is_not_writable(healthy, monkeypatch):
    monkeypatch.setattr(server, "_data_dir_writable", lambda: False)
    assert client.get("/api/health").status_code == 503


def test_health_reports_a_dead_analysis_worker(healthy, monkeypatch):
    monkeypatch.setattr(jobs, "worker_status", lambda: {"started": True, "alive": False,
                                                            "queued": 2, "running": "abc"})
    r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["checks"]["worker"]["ok"] is False


def test_worker_never_started_is_fine(healthy, monkeypatch):
    monkeypatch.setattr(jobs, "worker_status", lambda: {"started": False, "alive": None,
                                                            "queued": 0, "running": None})
    assert client.get("/api/health").json()["checks"]["worker"]["ok"] is True


def test_health_is_behind_the_same_auth(healthy, monkeypatch):
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD", "pw")
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD_BYTES", b"pw")
    remote = TestClient(server.app, client=("192.168.1.9", 5000))
    assert remote.get("/api/health", headers={"host": "x.example"}).status_code == 401


# ── logging ─────────────────────────────────────────────────────────────

@pytest.fixture
def clean_logger():
    lg = logging.getLogger("dinkiq")
    saved = lg.handlers[:]
    lg.handlers.clear()
    yield lg
    for h in lg.handlers:
        h.close()
    lg.handlers[:] = saved


def test_log_lines_are_timestamped_and_rotating(tmp_path, clean_logger):
    path = logsetup.configure_logging(tmp_path)
    clean_logger.warning("hello world")
    for h in clean_logger.handlers:
        h.flush()
    line = path.read_text().strip().splitlines()[-1]
    # 2026-11-16 08:21:10,123 WARNING dinkiq: hello world
    assert line[:4].isdigit() and line[4] == "-" and "WARNING" in line and "dinkiq" in line
    assert line.endswith("hello world")
    handler = next(h for h in clean_logger.handlers if h.__class__.__name__ == "RotatingFileHandler")
    assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 5


def test_configure_logging_is_idempotent(tmp_path, clean_logger):
    logsetup.configure_logging(tmp_path)
    logsetup.configure_logging(tmp_path)
    assert len([h for h in clean_logger.handlers if h.__class__.__name__ == "RotatingFileHandler"]) == 1


def test_unwritable_log_dir_does_not_stop_startup(clean_logger):
    assert logsetup.configure_logging(Path("/proc/definitely/not/writable")) is None


def test_uvicorn_log_config_rotates_and_timestamps(tmp_path):
    """Apply the real dictConfig and look at the file: an earlier version used
    the key `fmt` (only valid for uvicorn's own formatter classes), which
    dictConfig silently ignored, so access lines had no timestamps."""
    import logging.config
    cfg = logsetup.uvicorn_log_config(tmp_path)
    assert cfg["handlers"]["file"]["class"].endswith("RotatingFileHandler")
    assert cfg["handlers"]["file"]["maxBytes"] == 5 * 1024 * 1024
    # ONE handler for every logger: two rotating handlers on the same file
    # would each rotate it out from under the other
    for name in ("uvicorn", "uvicorn.access", "dinkiq"):
        assert cfg["loggers"][name]["handlers"] == ["file"]
    saved = {n: logging.getLogger(n).handlers[:] for n in ("uvicorn", "uvicorn.access", "dinkiq")}
    try:
        logging.config.dictConfig(cfg)
        logging.getLogger("uvicorn.access").info('127.0.0.1:5 - "GET /x HTTP/1.1" 200')
        logging.getLogger("dinkiq.pipeline").warning("from the app")
        for n in ("uvicorn.access", "dinkiq"):
            for h in logging.getLogger(n).handlers:
                h.flush()
        lines = (tmp_path / logsetup.LOG_NAME).read_text().splitlines()
    finally:
        for n, hs in saved.items():
            lg = logging.getLogger(n)
            for h in lg.handlers:
                h.close()
            lg.handlers[:] = hs
    assert len(lines) == 2
    for line in lines:
        assert line[:4].isdigit() and line[4] == "-", line  # starts with a timestamp
    assert "INFO" in lines[0] and "GET /x" in lines[0]
    assert "WARNING" in lines[1] and "dinkiq.pipeline" in lines[1] and lines[1].endswith("from the app")


def test_uvicorn_log_config_is_none_when_the_dir_is_unwritable():
    assert logsetup.uvicorn_log_config(Path("/proc/definitely/not/writable")) is None


# ── a missing ffmpeg/ffprobe is a clear error, not an orphan session ────

def _no_ffprobe(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")
    monkeypatch.setattr(media.subprocess, "run", boom)


def test_probe_video_raises_a_clear_error_when_ffprobe_is_missing(monkeypatch, tmp_path):
    _no_ffprobe(monkeypatch)
    with pytest.raises(media.ToolMissing, match="ffprobe"):
        media.probe_video(tmp_path / "x.mp4")


def test_run_raises_a_clear_error_when_ffmpeg_is_missing(monkeypatch):
    _no_ffprobe(monkeypatch)
    with pytest.raises(media.ToolMissing, match="PATH"):
        media._run(["ffmpeg", "-version"])


def test_finishing_an_upload_without_ffprobe_is_a_503_with_no_orphan(monkeypatch):
    _no_ffprobe(monkeypatch)
    uid = client.post("/api/upload/init", json={"filename": "c.mp4"}).json()["upload_id"]
    client.put(f"/api/upload/{uid}?offset=0", content=b"video bytes")
    r = client.post(f"/api/upload/{uid}/finish", json={"filename": "c.mp4"})
    assert r.status_code == 503 and "ffprobe" in r.json()["detail"]
    assert list(pipeline.SESSIONS.iterdir()) == []            # no half-made session
    assert server._upload_part(uid).read_bytes() == b"video bytes"  # upload kept: retry after fixing PATH


def test_replacing_a_video_without_ffprobe_is_a_503_and_keeps_the_original(monkeypatch):
    sdir = pipeline.SESSIONS / "tm1"
    sdir.mkdir(parents=True)
    (sdir / "raw.mp4").write_bytes(b"original")
    (sdir / "status.json").write_text(json.dumps({"stage": "done", "state": "done"}))
    uid = client.post("/api/upload/init", json={"filename": "n.mp4", "session_id": "tm1"}).json()["upload_id"]
    client.put(f"/api/upload/{uid}?offset=0", content=b"new")
    _no_ffprobe(monkeypatch)
    r = client.post(f"/api/upload/{uid}/finish", json={"filename": "n.mp4", "session_id": "tm1"})
    assert r.status_code == 503
    assert (sdir / "raw.mp4").read_bytes() == b"original" and not list(sdir.glob("incoming*"))


# ── ingest failures notify too ──────────────────────────────────────────

@pytest.fixture
def notes(monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "notify_webhook",
                        lambda sdir, ok, error=None, **kw: seen.append((sdir.name, ok, error, kw)))
    return seen


def test_unreadable_video_at_ingest_sends_a_failure_notification(monkeypatch, tmp_path, notes):
    sdir = tmp_path / "ing1"
    sdir.mkdir()
    raw = sdir / "raw.mp4"
    raw.write_bytes(b"junk")
    monkeypatch.setattr(media, "probe_video", lambda p: {
        "codec": None, "width": 0, "height": 0, "fps": 0.0, "frames": 0, "duration": 0.0})
    media.ingest(sdir, raw)
    assert session_status.get_status(sdir)["state"] == "error"
    assert len(notes) == 1 and notes[0][1] is False and "unreadable" in notes[0][2]


def test_ffmpeg_crash_at_ingest_sends_a_failure_notification(monkeypatch, tmp_path, notes):
    sdir = tmp_path / "ing2"
    sdir.mkdir()
    raw = sdir / "raw.mp4"
    raw.write_bytes(b"x")
    monkeypatch.setattr(media, "probe_video", lambda p: {
        "codec": "hevc", "width": 3840, "height": 2160, "fps": 60.0, "frames": 100, "duration": 2.0})

    def die(cmd):
        raise RuntimeError("ffmpeg failed: Invalid data found when processing input")
    monkeypatch.setattr(media, "_run", die)
    media.ingest(sdir, raw)
    assert session_status.get_status(sdir)["state"] == "error"
    assert len(notes) == 1 and notes[0][1] is False


def test_successful_ingest_does_not_notify(monkeypatch, tmp_path, notes):
    sdir = tmp_path / "ing3"
    sdir.mkdir()
    raw = sdir / "raw.mp4"
    raw.write_bytes(b"x")
    info = {"codec": "h264", "width": 1280, "height": 720, "fps": 30.0, "frames": 100, "duration": 3.3}
    monkeypatch.setattr(media, "probe_video", lambda p: info)
    monkeypatch.setattr(media, "_run", lambda cmd: None)
    monkeypatch.setattr(media, "_has_audio", lambda p: False)
    media.ingest(sdir, raw)
    assert session_status.get_status(sdir)["state"] == "done" and notes == []


def test_failure_message_says_what_failed():
    msg = notify.notify_message(Path("/nonexistent/sess"), ok=False, error="bad file", what="prepare the video")
    assert "failed to prepare the video" in msg and "bad file" in msg
    assert "failed to analyze" in notify.notify_message(Path("/nonexistent/sess"), ok=False, error="x")


def test_worker_status_reflects_the_queue():
    st = jobs.worker_status()
    assert set(st) == {"started", "alive", "queued", "running"}
    assert isinstance(st["queued"], int)


def test_subprocess_error_type_is_still_a_runtime_error():
    assert issubclass(media.ToolMissing, RuntimeError)
    assert subprocess  # (imported for the monkeypatches above)
