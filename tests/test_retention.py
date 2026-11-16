"""Storage retention and disk-usage reporting.

Every upload's raw file was kept forever alongside the normalized 720p copy
(on the sample, raw 10 MB + video 25 MB + audio 13 MB) and nothing showed how
much space sessions use. Raw is only read during ingest, so it can expire --
but it is the user's original footage, so after a grace period, not instantly.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import pipeline
import server

client = TestClient(server.app)
DAY = 86400


def _session(sid, raw_age_days=30, ingested=True, state="done", raw_name="raw.mp4",
             ingest_json=True, stage=None):
    d = pipeline.SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    raw = d / raw_name
    raw.write_bytes(b"r" * 1000)
    t = time.time() - raw_age_days * DAY
    os.utime(raw, (t, t))
    if ingested:
        (d / "video.mp4").write_bytes(b"v" * 500)
        if ingest_json:
            (d / "ingest.json").write_text("{}")
    stage = stage or ("done" if state == "done" else "tracking")
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": state}))
    return d


@pytest.fixture(autouse=True)
def _default_retention(monkeypatch):
    monkeypatch.delenv("DINKIQ_KEEP_RAW_DAYS", raising=False)


def test_old_raw_is_swept_once_ingest_is_complete():
    d = _session("old1", raw_age_days=30)
    server._maintenance_once()
    assert not (d / "raw.mp4").exists()
    assert (d / "video.mp4").exists() and (d / "ingest.json").exists()  # everything else stays


def test_recent_raw_is_kept_for_the_grace_period():
    d = _session("new1", raw_age_days=3)
    server._maintenance_once()
    assert (d / "raw.mp4").exists()


def test_default_grace_period_is_fourteen_days():
    a, b = _session("edge_old", raw_age_days=14.5), _session("edge_new", raw_age_days=13.5)
    server._maintenance_once()
    assert not (a / "raw.mp4").exists() and (b / "raw.mp4").exists()


def test_raw_is_kept_while_a_session_is_still_being_processed():
    d = _session("busy1", raw_age_days=60, state="running")
    server._maintenance_once()
    assert (d / "raw.mp4").exists()


def test_raw_is_kept_when_ingest_never_finished():
    """No normalized video yet: raw is the only copy of the footage."""
    d = _session("noing", raw_age_days=60, ingested=False)
    server._maintenance_once()
    assert (d / "raw.mp4").exists()


def test_sessions_that_predate_ingest_json_are_still_swept():
    """ingest.json is only metadata; the sample session (made before it existed)
    must not keep its raw upload forever just for lacking it."""
    d = _session("legacy1", raw_age_days=60, ingest_json=False)
    server._maintenance_once()
    assert not (d / "raw.mp4").exists() and (d / "video.mp4").exists()


def test_raw_is_kept_when_ingest_itself_failed():
    """A partial video.mp4 can exist after an ffmpeg crash: raw is the evidence
    (and the only good copy), so it stays."""
    d = _session("ingfail", raw_age_days=60, state="error", stage="ingest")
    server._maintenance_once()
    assert (d / "raw.mp4").exists()


def test_raw_is_swept_when_only_the_analysis_failed():
    """Ingest had finished, so the normalized video is complete."""
    d = _session("anfail", raw_age_days=60, state="error", stage="tracking")
    server._maintenance_once()
    assert not (d / "raw.mp4").exists()


def test_env_can_keep_raw_forever(monkeypatch):
    monkeypatch.setenv("DINKIQ_KEEP_RAW_DAYS", "-1")
    d = _session("keep1", raw_age_days=3650)
    server._maintenance_once()
    assert (d / "raw.mp4").exists()


def test_env_can_drop_raw_as_soon_as_ingest_is_done(monkeypatch):
    monkeypatch.setenv("DINKIQ_KEEP_RAW_DAYS", "0")
    d = _session("drop1", raw_age_days=0)
    server._maintenance_once()
    assert not (d / "raw.mp4").exists()


def test_env_custom_window_and_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DINKIQ_KEEP_RAW_DAYS", "2")
    a = _session("w_old", raw_age_days=3)
    b = _session("w_new", raw_age_days=1)
    server._maintenance_once()
    assert not (a / "raw.mp4").exists() and (b / "raw.mp4").exists()
    monkeypatch.setenv("DINKIQ_KEEP_RAW_DAYS", "banana")
    c = _session("w_bad", raw_age_days=3)
    server._maintenance_once()
    assert (c / "raw.mp4").exists()  # 14-day default applies


def test_every_raw_extension_is_covered_and_trash_is_untouched():
    mov = _session("mov1", raw_age_days=30, raw_name="raw.mov")
    trashed = pipeline.SESSIONS / ".trash" / "tr1"
    trashed.mkdir(parents=True)
    (trashed / "meta.json").write_text(json.dumps({"deleted_at": time.time()}))  # fresh: not purge-eligible
    (trashed / "raw.mp4").write_bytes(b"x")
    t = time.time() - 90 * DAY
    os.utime(trashed / "raw.mp4", (t, t))
    server._maintenance_once()
    assert not (mov / "raw.mov").exists()
    assert (trashed / "raw.mp4").exists()  # trash has its own 7-day purge


def test_a_session_with_no_raw_is_fine():
    d = _session("nr1", raw_age_days=30)
    (d / "raw.mp4").unlink()
    server._maintenance_once()  # must not raise


# ── disk usage reporting ────────────────────────────────────────────────

def test_sessions_list_reports_each_sessions_disk_usage():
    d = _session("du1", raw_age_days=1)
    (d / "clips").mkdir()
    (d / "clips" / "rally_00.mp4").write_bytes(b"c" * 250)
    row = next(s for s in client.get("/api/sessions").json() if s["session_id"] == "du1")
    # raw 1000 + video 500 + clip 250 + ingest.json "{}" 2 + status.json
    assert row["disk_bytes"] == 1000 + 500 + 250 + 2 + len((d / "status.json").read_bytes())


def test_disk_usage_shrinks_after_raw_is_swept():
    _session("du2", raw_age_days=30)
    before = next(s for s in client.get("/api/sessions").json() if s["session_id"] == "du2")["disk_bytes"]
    server._maintenance_once()
    after = next(s for s in client.get("/api/sessions").json() if s["session_id"] == "du2")["disk_bytes"]
    assert before - after == 1000


def test_health_reports_total_session_storage(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(server, "_weights_present", lambda: True)
    _session("du3", raw_age_days=1)
    body = client.get("/api/health").json()
    assert body["sessions_bytes"] >= 1500
