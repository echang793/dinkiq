"""The highlight reel is built in the background: POST starts it, status reports it, GET serves it.

Building used to happen inside the GET, so a phone sat on a spinner with no way to tell whether
anything was happening (and a second tap started a second build).
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import media
import reel
import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _session(sid="r", stage="done"):
    d = SESSIONS / sid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"stage": stage, "state": stage}))
    (d / "meta.json").write_text("{}")
    return d


def _wait(sid, state, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/api/session/{sid}/highlights/status").json()
        if body["state"] == state:
            return body
        time.sleep(0.02)
    raise AssertionError(f"never reached {state}: {body}")


@pytest.fixture(autouse=True)
def _fresh():
    reel.reset()
    yield
    reel.reset()


def test_status_is_none_before_anything_was_requested():
    d = _session()
    assert client.get(f"/api/session/{d.name}/highlights/status").json()["state"] == "none"


def test_post_builds_in_the_background_and_the_reel_can_then_be_fetched(monkeypatch):
    d = _session()
    gate = threading.Event()

    def slow_build(sdir):
        gate.wait(2)
        (sdir / "highlights.mp4").write_bytes(b"reel")
        return sdir / "highlights.mp4"

    monkeypatch.setattr(media, "build_highlights", slow_build)
    r = client.post(f"/api/session/{d.name}/highlights")
    assert r.status_code == 202 and r.json()["state"] == "building"
    assert client.get(f"/api/session/{d.name}/highlights/status").json()["state"] == "building"
    gate.set()
    _wait(d.name, "ready")
    assert client.get(f"/api/session/{d.name}/highlights.mp4").content == b"reel"


def test_asking_again_while_building_does_not_start_a_second_build(monkeypatch):
    d = _session()
    gate, calls = threading.Event(), []

    def build(sdir):
        calls.append(1)
        gate.wait(2)
        (sdir / "highlights.mp4").write_bytes(b"x")
        return sdir / "highlights.mp4"

    monkeypatch.setattr(media, "build_highlights", build)
    client.post(f"/api/session/{d.name}/highlights")
    client.post(f"/api/session/{d.name}/highlights")
    gate.set()
    _wait(d.name, "ready")
    assert len(calls) == 1


def test_an_already_built_reel_is_ready_immediately(monkeypatch):
    d = _session()
    (d / "highlights.mp4").write_bytes(b"old")
    monkeypatch.setattr(media, "build_highlights", lambda s: pytest.fail("rebuilt a cached reel"))
    r = client.post(f"/api/session/{d.name}/highlights")
    assert r.status_code == 200 and r.json()["state"] == "ready"


def test_no_rally_clips_is_reported_as_an_error_and_can_be_retried(monkeypatch):
    d = _session()
    monkeypatch.setattr(media, "build_highlights", lambda s: None)
    client.post(f"/api/session/{d.name}/highlights")
    body = _wait(d.name, "error")
    assert "clip" in body["error"]

    def ok(sdir):
        (sdir / "highlights.mp4").write_bytes(b"x")
        return sdir / "highlights.mp4"

    monkeypatch.setattr(media, "build_highlights", ok)
    client.post(f"/api/session/{d.name}/highlights")
    _wait(d.name, "ready")


def test_a_crashing_build_becomes_an_error_state_not_a_dead_thread(monkeypatch):
    d = _session()

    def boom(sdir):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(media, "build_highlights", boom)
    client.post(f"/api/session/{d.name}/highlights")
    assert "ffmpeg" in _wait(d.name, "error")["error"]


def test_a_session_that_is_not_done_cannot_build_a_reel():
    d = _session(stage="tracking")
    assert client.post(f"/api/session/{d.name}/highlights").status_code == 409


def test_recalibrating_discards_the_reel_and_the_status_follows():
    d = _session()
    (d / "highlights.mp4").write_bytes(b"old")
    assert client.get(f"/api/session/{d.name}/highlights/status").json()["state"] == "ready"
    (d / "highlights.mp4").unlink()                     # what clear_derived does
    assert client.get(f"/api/session/{d.name}/highlights/status").json()["state"] == "none"
