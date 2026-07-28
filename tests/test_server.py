"""Server endpoint tests: bulk delete safety (path traversal, missing ids)."""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
from pipeline import SESSIONS

client = TestClient(server.app)


def _mk(sid: str) -> Path:
    d = SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text('{"filename": "t.mp4"}')
    return d


def test_bulk_delete():
    a, b = _mk("testdel_aaa"), _mk("testdel_bbb")
    try:
        r = client.post("/api/sessions/delete",
                        json={"ids": ["testdel_aaa", "testdel_bbb"]})
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["deleted"]) == ["testdel_aaa", "testdel_bbb"]
        assert not a.exists() and not b.exists()  # gone from live sessions (soft-deleted to trash)
    finally:
        import shutil
        shutil.rmtree(SESSIONS / ".trash" / "testdel_aaa", ignore_errors=True)
        shutil.rmtree(SESSIONS / ".trash" / "testdel_bbb", ignore_errors=True)


def test_delete_rejects_traversal_and_missing():
    keep = _mk("testdel_keep")
    try:
        r = client.post("/api/sessions/delete",
                        json={"ids": ["../testdel_keep", "no_such_session"]})
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] == []
        assert {e["error"] for e in body["errors"]} == {"bad session id", "not found"}
        assert keep.exists()  # traversal attempt didn't touch a sibling
    finally:
        import shutil
        shutil.rmtree(keep, ignore_errors=True)


def test_delete_empty_ids_rejected():
    r = client.post("/api/sessions/delete", json={"ids": []})
    assert r.status_code == 422  # pydantic min_length


def test_soft_delete_then_restore():
    d = _mk("testtrash_a")
    try:
        r = client.post("/api/sessions/delete", json={"ids": ["testtrash_a"]})
        assert r.status_code == 200 and r.json()["deleted"] == ["testtrash_a"]
        assert not d.exists()  # gone from live sessions dir
        ids = {row["session_id"] for row in client.get("/api/sessions").json()}
        assert "testtrash_a" not in ids
        trash_ids = {row["session_id"] for row in client.get("/api/sessions/trash").json()}
        assert "testtrash_a" in trash_ids

        r = client.post("/api/sessions/restore", json={"ids": ["testtrash_a"]})
        assert r.status_code == 200 and r.json()["restored"] == ["testtrash_a"]
        assert d.exists()
        ids = {row["session_id"] for row in client.get("/api/sessions").json()}
        assert "testtrash_a" in ids
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(SESSIONS / ".trash" / "testtrash_a", ignore_errors=True)


def test_meta_patch_notes_and_opponent():
    d = _mk("testmeta_a")
    try:
        r = client.patch("/api/session/testmeta_a/meta",
                         json={"notes": "played well at kitchen", "opponent": "Sam"})
        assert r.status_code == 200
        row = next(row for row in client.get("/api/sessions").json()
                  if row["session_id"] == "testmeta_a")
        assert row["notes"] == "played well at kitchen"
        assert row["opponent"] == "Sam"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_sessions_filter_by_label_and_opponent():
    a = _mk("testfilt_a")
    b = _mk("testfilt_b")
    try:
        client.patch("/api/session/testfilt_a/meta", json={"label": "Morning drills", "opponent": "Sam"})
        client.patch("/api/session/testfilt_b/meta", json={"label": "Evening match", "opponent": "Alex"})
        r = client.get("/api/sessions", params={"label": "morning"})
        ids = {row["session_id"] for row in r.json()}
        assert ids == {"testfilt_a"}
        r = client.get("/api/sessions", params={"opponent": "alex"})
        ids = {row["session_id"] for row in r.json()}
        assert ids == {"testfilt_b"}
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_meta_patch_context_accepts_valid_and_rejects_invalid():
    d = _mk("testctx_a")
    try:
        r = client.patch("/api/session/testctx_a/meta", json={"context": "league"})
        assert r.status_code == 200
        row = next(row for row in client.get("/api/sessions").json()
                  if row["session_id"] == "testctx_a")
        assert row["context"] == "league"
        r = client.patch("/api/session/testctx_a/meta", json={"context": "pickup"})
        assert r.status_code == 422
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_sessions_filter_by_context():
    a = _mk("testctx_practice")
    b = _mk("testctx_tourney")
    try:
        client.patch("/api/session/testctx_practice/meta", json={"context": "practice"})
        client.patch("/api/session/testctx_tourney/meta", json={"context": "tournament"})
        r = client.get("/api/sessions", params={"context": "tournament"})
        ids = {row["session_id"] for row in r.json()}
        assert ids == {"testctx_tourney"}
    finally:
        import shutil
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_reupload_replaces_video_and_clears_derived(monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline, "ingest", lambda sdir, raw: None)  # skip real ffmpeg
    d = _mk("testreup_a")
    (d / "video.mp4").write_bytes(b"old")
    (d / "metrics.json").write_text("{}")
    try:
        r = client.post("/api/session/testreup_a/reupload",
                        files={"file": ("clip2.mp4", b"fake bytes", "video/mp4")})
        assert r.status_code == 200
        assert r.json()["session_id"] == "testreup_a"
        assert not (d / "video.mp4").exists()  # cleared for re-ingest
        assert not (d / "metrics.json").exists()  # derived artifacts cleared
        assert (d / "raw.mp4").exists()
        meta = json.loads((d / "meta.json").read_text())
        assert meta["filename"] == "clip2.mp4"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_subject_marker_returns_earliest_frame_box():
    d = _mk("testmarker_a")
    try:
        (d / "metrics.json").write_text(json.dumps(
            {"subject_track_id": 1, "opponent_track_id": 2}))
        df = pd.DataFrame([
            {"frame": 4, "track_id": 1, "x1": 10, "y1": 20, "x2": 30, "y2": 80,
             "conf": .9, "lwx": 0, "lwy": 0, "rwx": 0, "rwy": 0},
            {"frame": 6, "track_id": 1, "x1": 12, "y1": 22, "x2": 32, "y2": 82,
             "conf": .9, "lwx": 0, "lwy": 0, "rwx": 0, "rwy": 0},
            {"frame": 0, "track_id": 2, "x1": 100, "y1": 20, "x2": 130, "y2": 90,
             "conf": .9, "lwx": 0, "lwy": 0, "rwx": 0, "rwy": 0},
        ])
        df.to_parquet(d / "tracks.parquet", index=False)

        r = client.get("/api/session/testmarker_a/subject-marker")
        assert r.status_code == 200
        body = r.json()
        assert body["subject"] == {"frame": 4, "x1": 10.0, "y1": 20.0, "x2": 30.0, "y2": 80.0}
        assert body["opponent"] == {"frame": 0, "x1": 100.0, "y1": 20.0, "x2": 130.0, "y2": 90.0}
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_subject_marker_no_tracks_parquet_degrades():
    d = _mk("testmarker_notracks")
    try:
        (d / "metrics.json").write_text(json.dumps(
            {"subject_track_id": 1, "opponent_track_id": 2}))
        r = client.get("/api/session/testmarker_notracks/subject-marker")
        assert r.status_code == 200
        assert r.json() == {"subject": None, "partner": None, "opponent": None, "opponents": []}
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_subject_marker_null_opponent_id_degrades():
    d = _mk("testmarker_noopp")
    try:
        (d / "metrics.json").write_text(json.dumps(
            {"subject_track_id": 1, "opponent_track_id": None}))
        df = pd.DataFrame([
            {"frame": 0, "track_id": 1, "x1": 10, "y1": 20, "x2": 30, "y2": 80,
             "conf": .9, "lwx": 0, "lwy": 0, "rwx": 0, "rwy": 0},
        ])
        df.to_parquet(d / "tracks.parquet", index=False)
        r = client.get("/api/session/testmarker_noopp/subject-marker")
        assert r.status_code == 200
        body = r.json()
        assert body["subject"]["frame"] == 0
        assert body["opponent"] is None
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_subject_marker_missing_session_404s():
    r = client.get("/api/session/does_not_exist_xyz/subject-marker")
    assert r.status_code == 404


def test_upload_rejects_bad_match_type():
    r = client.post("/api/upload", files={"file": ("clip.mp4", b"x", "video/mp4")},
                    data={"match_type": "triples"})
    assert r.status_code == 400


def test_calibrate_doubles_requires_partner_px():
    d = _mk("testcal_doubles")
    try:
        (d / "meta.json").write_text(json.dumps({"filename": "t.mp4", "match_type": "doubles"}))
        (d / "status.json").write_text(json.dumps({"stage": "ingest", "state": "done"}))
        r = client.post("/api/session/testcal_doubles/calibrate", json={
            "corners_px": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "self_px": [0.5, 0.5],
        })
        assert r.status_code == 400
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_subject_marker_doubles_returns_partner_and_two_opponents():
    d = _mk("testmarker_doubles")
    try:
        (d / "metrics.json").write_text(json.dumps({
            "subject_track_id": 1, "partner_track_id": 2,
            "opponent_track_ids": [3, 4], "opponent_track_id": 3,
        }))
        df = pd.DataFrame([
            {"frame": 0, "track_id": tid, "x1": tid * 10.0, "y1": 0.0, "x2": tid * 10.0 + 5,
             "y2": 50.0, "conf": .9, "lwx": 0, "lwy": 0, "rwx": 0, "rwy": 0}
            for tid in (1, 2, 3, 4)
        ])
        df.to_parquet(d / "tracks.parquet", index=False)
        r = client.get("/api/session/testmarker_doubles/subject-marker")
        assert r.status_code == 200
        body = r.json()
        assert body["subject"]["x1"] == 10.0
        assert body["partner"]["x1"] == 20.0
        assert len(body["opponents"]) == 2
        assert {b["x1"] for b in body["opponents"]} == {30.0, 40.0}
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_calibrate_writes_last_calibration_corners_only(monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline, "enqueue_analyze", lambda sdir: None)
    d = _mk("testcal_last")
    (d / "status.json").write_text(json.dumps({"stage": "ingest", "state": "done"}))
    try:
        r = client.post("/api/session/testcal_last/calibrate", json={
            "corners_px": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "kitchen_px": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
            "self_px": [0.5, 0.5],
        })
        assert r.status_code == 200
        last = client.get("/api/last-calibration").json()
        assert last["available"] is True
        assert last["corners_px"] == [[0, 0], [1, 0], [1, 1], [0, 1]]
        assert last["kitchen_px"] == [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
        assert "self_px" not in last  # per-person click never persisted
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        server.LAST_CALIBRATION_PATH.unlink(missing_ok=True)


def test_last_calibration_unavailable_when_never_set():
    import server
    server.LAST_CALIBRATION_PATH.unlink(missing_ok=True)
    r = client.get("/api/last-calibration")
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_index_has_no_base_when_served_directly():
    """Served straight off localhost/LAN there is no proxy prefix, so the
    page must stay exactly as-is and all URLs stay root-absolute."""
    r = client.get("/")
    assert r.status_code == 200
    # the page's own `const BASE = window.__BASE__ || ''` always mentions the
    # global -- what must be absent is the server-injected assignment
    assert "<script>window.__BASE__=" not in r.text


def test_index_injects_base_from_forwarded_prefix():
    """Behind the vantage Caddy hub the app is mounted at /app/dinkiq/ with
    the prefix stripped before it reaches us -- without echoing it back to
    the page, the frontend's root-absolute fetch('/api/...') resolves
    against the hub origin and 404s."""
    r = client.get("/", headers={"X-Forwarded-Prefix": "/app/dinkiq"})
    assert r.status_code == 200
    assert '<script>window.__BASE__="/app/dinkiq";</script>' in r.text
    # injected inside <head>, before the app's own script runs
    assert r.text.index("window.__BASE__") < r.text.index("const BASE")


def test_index_strips_trailing_slash_from_prefix():
    # BASE is concatenated as BASE + '/api/...', so a trailing slash would
    # produce a double slash in every request path
    r = client.get("/", headers={"X-Forwarded-Prefix": "/app/dinkiq/"})
    assert '<script>window.__BASE__="/app/dinkiq";</script>' in r.text


if __name__ == "__main__":
    for fn in [test_bulk_delete, test_delete_rejects_traversal_and_missing,
               test_delete_empty_ids_rejected, test_soft_delete_then_restore,
               test_meta_patch_notes_and_opponent, test_sessions_filter_by_label_and_opponent,
               test_subject_marker_returns_earliest_frame_box,
               test_subject_marker_no_tracks_parquet_degrades,
               test_subject_marker_null_opponent_id_degrades,
               test_subject_marker_missing_session_404s,
               test_subject_marker_doubles_returns_partner_and_two_opponents,
               test_upload_rejects_bad_match_type, test_calibrate_doubles_requires_partner_px,
               test_meta_patch_context_accepts_valid_and_rejects_invalid,
               test_sessions_filter_by_context,
               test_calibrate_writes_last_calibration_corners_only,
               test_last_calibration_unavailable_when_never_set,
               test_index_has_no_base_when_served_directly,
               test_index_injects_base_from_forwarded_prefix,
               test_index_strips_trailing_slash_from_prefix]:
        fn()
        print(f"ok {fn.__name__}")
