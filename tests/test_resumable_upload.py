"""Resumable chunked upload: init / append / resume / finish.

A phone on cellular pushing a multi-GB clip through one POST loses the
whole transfer on a single dropped connection; these endpoints let it
append in chunks and ask the server where to pick up again.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def _init(filename="clip.mp4", match_type="singles"):
    r = client.post("/api/upload/init", json={"filename": filename, "match_type": match_type})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _cleanup(sid=None, upload_id=None):
    if sid:
        shutil.rmtree(server.SESSIONS / sid, ignore_errors=True)
    if upload_id:
        server._upload_part(upload_id).unlink(missing_ok=True)


def test_init_rejects_unsupported_extension():
    r = client.post("/api/upload/init", json={"filename": "notes.txt"})
    assert r.status_code == 400


def test_init_rejects_bad_match_type():
    r = client.post("/api/upload/init", json={"filename": "clip.mp4", "match_type": "triples"})
    assert r.status_code == 422


def test_chunks_append_in_order():
    uid = _init()
    try:
        r = client.put(f"/api/upload/{uid}?offset=0", content=b"AAAA")
        assert r.json()["offset"] == 4
        r = client.put(f"/api/upload/{uid}?offset=4", content=b"BBB")
        assert r.json()["offset"] == 7
        assert server._upload_part(uid).read_bytes() == b"AAAABBB"
    finally:
        _cleanup(upload_id=uid)


def test_status_reports_offset_for_resume():
    """After a dropped connection the client asks where the server got to
    rather than restarting the whole transfer."""
    uid = _init()
    try:
        client.put(f"/api/upload/{uid}?offset=0", content=b"0123456789")
        r = client.get(f"/api/upload/{uid}")
        assert r.status_code == 200 and r.json()["offset"] == 10
    finally:
        _cleanup(upload_id=uid)


def test_wrong_offset_is_rejected_and_reports_truth():
    """A retried chunk must not silently duplicate into the file; the 409
    tells the client the real offset so it can re-slice and continue."""
    uid = _init()
    try:
        client.put(f"/api/upload/{uid}?offset=0", content=b"AAAA")
        r = client.put(f"/api/upload/{uid}?offset=0", content=b"AAAA")  # retry
        assert r.status_code == 409
        assert "4" in r.json()["detail"]
        assert server._upload_part(uid).read_bytes() == b"AAAA"  # unchanged
    finally:
        _cleanup(upload_id=uid)


def test_resume_after_simulated_drop_produces_intact_file():
    payload = bytes(range(256)) * 40
    uid = _init()
    try:
        client.put(f"/api/upload/{uid}?offset=0", content=payload[:5000])
        # connection "drops" — client re-queries and resumes from there
        at = client.get(f"/api/upload/{uid}").json()["offset"]
        client.put(f"/api/upload/{uid}?offset={at}", content=payload[at:])
        assert server._upload_part(uid).read_bytes() == payload
    finally:
        _cleanup(upload_id=uid)


def test_unknown_upload_id_404s():
    assert client.get("/api/upload/" + "a" * 32).status_code == 404
    assert client.put("/api/upload/" + "a" * 32 + "?offset=0", content=b"x").status_code == 404


def test_path_traversal_upload_id_rejected():
    """upload_id becomes a filesystem path, so anything but our own uuid
    hex must be refused outright."""
    for bad in ["../../etc/passwd", "..", "abc", "A" * 32]:
        r = client.get(f"/api/upload/{bad}")
        assert r.status_code in (400, 404), (bad, r.status_code)


def test_finish_creates_session_and_starts_ingest(monkeypatch):
    started = []
    monkeypatch.setattr(server.threading, "Thread",
                        lambda target, args, daemon: type(
                            "T", (), {"start": lambda self: started.append(args)})())
    monkeypatch.setattr(server.pipeline, "probe_video", lambda p: {"duration": 12.0})
    uid = _init()
    sid = None
    try:
        client.put(f"/api/upload/{uid}?offset=0", content=b"fake video bytes")
        r = client.post(f"/api/upload/{uid}/finish",
                        json={"filename": "clip.mp4", "match_type": "doubles"})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        sdir = server.SESSIONS / sid
        assert (sdir / "raw.mp4").read_bytes() == b"fake video bytes"
        assert not server._upload_part(uid).exists()  # partial consumed
        import json as _json
        assert _json.loads((sdir / "meta.json").read_text())["match_type"] == "doubles"
        assert started, "ingest was not started"
    finally:
        _cleanup(sid=sid, upload_id=uid)


def test_finish_rejects_empty_upload():
    uid = _init()
    try:
        r = client.post(f"/api/upload/{uid}/finish", json={"filename": "clip.mp4"})
        assert r.status_code == 400
    finally:
        _cleanup(upload_id=uid)


def test_capture_hint_flags_oversized_capture():
    """Analysis runs at 720p30 regardless, so anything larger costs upload
    time and transcode for identical results."""
    assert server.capture_hint({"height": 2160, "fps": 60.0}) is not None
    assert "720p30" in server.capture_hint({"height": 2160, "fps": 60.0})
    assert server.capture_hint({"height": 1080, "fps": 60.0}) is not None   # fps alone
    assert server.capture_hint({"height": 2160, "fps": 30.0}) is not None   # height alone


def test_capture_hint_quiet_for_reasonable_footage():
    assert server.capture_hint({"height": 1080, "fps": 30.0}) is None
    assert server.capture_hint({"height": 720, "fps": 30.0}) is None
    assert server.capture_hint({}) is None


def test_finish_surfaces_capture_hint(monkeypatch):
    monkeypatch.setattr(server.threading, "Thread",
                        lambda target, args, daemon: type(
                            "T", (), {"start": lambda self: None})())
    monkeypatch.setattr(server.pipeline, "probe_video",
                        lambda p: {"duration": 10.0, "height": 2160, "fps": 60.0})
    uid = _init()
    sid = None
    try:
        client.put(f"/api/upload/{uid}?offset=0", content=b"x")
        r = client.post(f"/api/upload/{uid}/finish", json={"filename": "clip.mp4"})
        sid = r.json()["session_id"]
        assert "720p30" in r.json()["capture_hint"]
        import json as _json
        assert "capture_hint" in _json.loads(
            (server.SESSIONS / sid / "meta.json").read_text())
    finally:
        _cleanup(sid=sid, upload_id=uid)


if __name__ == "__main__":
    print("run via pytest")
