"""Upload hardening: disk space, partial caps, declared size, periodic sweeps.

A 2 GB-per-upload limit means little if nothing checks the disk has room, any
number of partials can be opened, and abandoned ones are only swept when
somebody happens to start another upload.
"""

import os
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from api import maintenance
from api import uploads
from api import common
import shutil

client = TestClient(server.app)
Usage = namedtuple("Usage", "total used free")
GB = 1 << 30


def _init(**body):
    return client.post("/api/upload/init", json={"filename": "c.mp4", **body})


def _age(uid, seconds):
    f = uploads._upload_part(uid)
    t = time.time() - seconds
    os.utime(f, (t, t))


# ── disk space ──────────────────────────────────────────────────────────

def test_init_refuses_when_the_declared_size_will_not_fit(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(500 * GB, 450 * GB, 3 * GB))
    r = _init(size=2 * GB)  # 3 GB free - 2 GB upload leaves less than the reserve
    assert r.status_code == 507 and "disk space" in r.json()["detail"]
    assert not list(common.UPLOADS.glob("*.part"))  # nothing created


def test_init_accepts_when_there_is_room(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(500 * GB, 100 * GB, 400 * GB))
    assert _init(size=2 * GB).status_code == 200


def test_init_without_a_declared_size_still_requires_the_reserve(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(500 * GB, 499 * GB, GB // 2))
    assert _init().status_code == 507


def test_a_chunk_is_refused_once_the_disk_is_nearly_full(monkeypatch):
    uid = _init().json()["upload_id"]
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(500 * GB, 499 * GB, GB // 2))
    r = client.put(f"/api/upload/{uid}?offset=0", content=b"data")
    assert r.status_code == 507
    assert uploads._upload_part(uid).stat().st_size == 0  # nothing half-written


def test_declared_size_over_the_limit_is_rejected_immediately():
    r = _init(size=uploads.MAX_UPLOAD_BYTES + 1)
    assert r.status_code == 413  # don't make the phone send 2 GB to find out


# ── concurrent partial cap ──────────────────────────────────────────────

def test_too_many_open_uploads_are_refused(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_PARTIAL_UPLOADS", 2)
    assert _init().status_code == 200 and _init().status_code == 200
    r = _init()
    assert r.status_code == 429 and "in progress" in r.json()["detail"]


def test_idle_partials_are_reclaimed_to_make_room(monkeypatch):
    """A phone that gave up mid-upload must not lock you out of uploading."""
    monkeypatch.setattr(uploads, "MAX_PARTIAL_UPLOADS", 2)
    a = _init().json()["upload_id"]
    b = _init().json()["upload_id"]
    _age(a, 2 * 3600)  # idle for two hours
    assert _init().status_code == 200
    assert not uploads._upload_part(a).exists() and uploads._upload_part(b).exists()


def test_active_partials_are_never_reclaimed(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_PARTIAL_UPLOADS", 1)
    a = _init().json()["upload_id"]
    assert _init().status_code == 429
    assert uploads._upload_part(a).exists()


# ── periodic maintenance ────────────────────────────────────────────────

def test_maintenance_sweeps_stale_uploads_and_expired_trash():
    uid = _init().json()["upload_id"]
    _age(uid, uploads.UPLOAD_TTL_S + 60)
    (common.TRASH / "old1").mkdir(parents=True)
    (common.TRASH / "old1" / "meta.json").write_text('{"deleted_at": 1}')
    maintenance.maintenance_once()
    assert not uploads._upload_part(uid).exists()
    assert not (common.TRASH / "old1").exists()


def test_maintenance_runs_in_the_background_after_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "MAINTENANCE_INTERVAL_S", 0.05)
    monkeypatch.setattr(maintenance, "maintenance_once", lambda: calls.append(1))
    with TestClient(server.app):
        deadline = time.time() + 3
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
    assert len(calls) >= 2


def test_a_failing_maintenance_pass_does_not_kill_the_loop(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(maintenance, "MAINTENANCE_INTERVAL_S", 0.05)
    monkeypatch.setattr(maintenance, "maintenance_once", flaky)
    with TestClient(server.app):
        deadline = time.time() + 3
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.05)
    assert len(calls) >= 3


@pytest.mark.parametrize("path", ["/api/upload/init"])
def test_init_requires_a_filename(path):
    assert client.post(path, json={}).status_code == 422
