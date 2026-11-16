"""Crash-safe JSON persistence + NaN-safe responses.

Everything the pipeline produces is a JSON file written with plain
write_text(): a crash or full disk mid-write left a truncated file, and since
readers called json.loads with no handling, ONE bad meta.json made every list
endpoint 500 at once. And a NaN reaching a response either 500s (Starlette
refuses to serialize it) or, worse elsewhere, is invalid JSON on iOS Safari.
"""

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import jsonio
import pipeline
import media
import jobs
import session_status
import server
from api import sessions as sessions_api

client = TestClient(server.app)


def _mk(sid, meta=None, status=None, **files):
    d = pipeline.SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta if meta is not None else {"filename": f"{sid}.mp4"}))
    if status:
        (d / "status.json").write_text(json.dumps(status))
    for name, content in files.items():
        (d / name.replace("__", ".")).write_text(content)
    return d


# ── jsonio unit tests ────────────────────────────────────────────────────

def test_atomic_write_roundtrip_and_no_temp_leftovers(tmp_path):
    f = tmp_path / "a.json"
    jsonio.atomic_write_json(f, {"x": 1, "y": [1, 2]})
    assert json.loads(f.read_text()) == {"x": 1, "y": [1, 2]}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_failed_write_keeps_the_old_file_intact(tmp_path):
    """The whole point: a crash mid-write must not destroy the good copy."""
    f = tmp_path / "a.json"
    jsonio.atomic_write_json(f, {"good": True})
    with pytest.raises(TypeError):
        jsonio.atomic_write_json(f, {"bad": object()})
    assert json.loads(f.read_text()) == {"good": True}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]  # temp cleaned up


def test_sanitize_turns_nan_inf_into_null_and_casts_numpy():
    out = jsonio.sanitize({
        "nan": float("nan"), "inf": float("inf"), "ninf": -math.inf,
        "np_f": np.float64(1.5), "np_nan": np.float32("nan"), "np_i": np.int64(3),
        "np_b": np.bool_(True), "arr": np.array([1.0, np.nan]),
        "nested": [{"v": float("nan")}], "tup": (1, float("inf")), "ok": 2.5, "s": "x", "n": None,
    })
    assert out == {"nan": None, "inf": None, "ninf": None, "np_f": 1.5, "np_nan": None,
                   "np_i": 3, "np_b": True, "arr": [1.0, None],
                   "nested": [{"v": None}], "tup": [1, None], "ok": 2.5, "s": "x", "n": None}
    json.dumps(out, allow_nan=False)  # strict-JSON safe


def test_atomic_write_never_emits_nan_literal(tmp_path):
    f = tmp_path / "a.json"
    jsonio.atomic_write_json(f, {"v": float("nan")})
    assert "NaN" not in f.read_text()
    assert json.loads(f.read_text()) == {"v": None}


def test_load_json_missing_returns_default_and_is_silent(tmp_path, caplog):
    assert jsonio.load_json(tmp_path / "nope.json") == {}
    assert jsonio.load_json(tmp_path / "nope.json", default=[]) == []
    assert not caplog.records


def test_load_json_corrupt_returns_default_and_warns(tmp_path, caplog):
    f = tmp_path / "bad.json"
    f.write_text('{"truncated": ')
    with caplog.at_level("WARNING"):
        assert jsonio.load_json(f) == {}
    assert any("bad.json" in r.getMessage() for r in caplog.records)


def test_load_json_default_is_not_shared_between_calls(tmp_path):
    a = jsonio.load_json(tmp_path / "x.json")
    a["mutated"] = 1
    assert jsonio.load_json(tmp_path / "x.json") == {}


def test_load_json_on_corrupt_overrides_default(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{")
    assert jsonio.load_json(f, default={"a": 1}, on_corrupt={"b": 2}) == {"b": 2}
    assert jsonio.load_json(tmp_path / "missing.json", default={"a": 1}, on_corrupt={"b": 2}) == {"a": 1}


# ── one corrupt file must not take the app down ─────────────────────────

def test_corrupt_meta_does_not_break_any_list_endpoint():
    _mk("good1", {"filename": "g.mp4", "label": "good"}, {"stage": "done", "state": "done"})
    bad = _mk("bad1", status={"stage": "done", "state": "done"})
    (bad / "meta.json").write_text('{"filename": "tru')  # truncated mid-write
    for path in ("/api/sessions", "/api/progress", "/api/leaderboard", "/api/streak",
                 "/api/sessions/export.csv", "/api/sessions/trash",
                 "/api/opponents/anyone/history", "/api/opponents/anyone/scouting"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
    ids = {s["session_id"] for s in client.get("/api/sessions").json()}
    assert ids == {"good1", "bad1"}  # the damaged one is still listed, not silently dropped


def test_corrupt_artifact_json_does_not_500_the_session_endpoint():
    d = _mk("bad2", status={"stage": "done", "state": "done"})
    (d / "metrics.json").write_text("{not json")
    (d / "dupr.json").write_text("")
    r = client.get("/api/session/bad2")
    assert r.status_code == 200
    assert r.json()["session_id"] == "bad2"


def test_corrupt_status_is_reported_as_error_so_the_user_can_reprocess():
    d = _mk("bad3")
    (d / "status.json").write_text('{"stage": "tra')
    st = session_status.get_status(d)
    assert st["state"] == "error" and "unreadable" in st["error"]
    # state "error" is what unlocks recalibrate/reprocess in the UI
    assert sessions_api._calibratable(st)


def test_missing_status_is_still_new_idle():
    assert session_status.get_status(_mk("fresh")) == {"stage": "new", "state": "idle"}


def test_set_status_is_atomic_no_temp_files(tmp_path):
    session_status.set_status(tmp_path, "tracking", "running", progress=0.5)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]


# ── NaN never reaches the client ────────────────────────────────────────

def test_nan_in_stored_json_comes_back_as_null_not_a_500():
    d = _mk("nan1", status={"stage": "done", "state": "done"})
    # json.dumps emits bare NaN by default, and json.loads reads it back --
    # exactly how a NaN metric already on disk would behave
    (d / "metrics.json").write_text(json.dumps({"zone_pct": {"kitchen": float("nan")},
                                                "distance_ft": float("inf")}))
    r = client.get("/api/session/nan1")
    assert r.status_code == 200
    m = r.json()["metrics"]
    assert m["zone_pct"]["kitchen"] is None and m["distance_ft"] is None
    assert "NaN" not in r.text and "Infinity" not in r.text


def test_nan_in_compare_and_progress_is_null():
    for sid in ("nan2", "nan3"):
        d = _mk(sid, status={"stage": "done", "state": "done"})
        (d / "metrics.json").write_text(json.dumps({"zone_pct": {"kitchen": float("nan")}}))
    assert client.get("/api/progress").status_code == 200
    r = client.get("/api/compare?a=nan2&b=nan3")
    assert r.status_code == 200 and "NaN" not in r.text


# ── calibration input is validated ──────────────────────────────────────

def _ready_session(sid="cal1"):
    return _mk(sid, status={"stage": "ingest", "state": "done"})


GOOD = {"corners_px": [[100, 100], [900, 100], [950, 600], [50, 600]], "self_px": [500, 500]}


def test_calibrate_rejects_nan_and_infinity():
    _ready_session()
    for bad in ('{"corners_px": [[NaN,0],[1,0],[1,1],[0,1]], "self_px": [1,1]}',
                '{"corners_px": [[0,0],[Infinity,0],[1,1],[0,1]], "self_px": [1,1]}',
                '{"corners_px": [[0,0],[1,0],[1,1],[0,1]], "self_px": [1,NaN]}'):
        r = client.post("/api/session/cal1/calibrate", content=bad,
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, bad


def test_calibrate_rejects_malformed_points():
    _ready_session()
    for bad in ({**GOOD, "corners_px": [[1, 2, 3], [1, 2], [1, 2], [1, 2]]},   # 3-vector
                {**GOOD, "corners_px": [[1], [1, 2], [1, 2], [1, 2]]},          # 1-vector
                {**GOOD, "self_px": [1e12, 5]},                                  # absurd magnitude
                {**GOOD, "kitchen_px": [[0, 0], [1, 1], [2, 2], [float(1e12), 0]]}):
        r = client.post("/api/session/cal1/calibrate", json=bad)
        assert r.status_code == 422, bad


def test_calibrate_accepts_corners_slightly_outside_the_frame(monkeypatch):
    """Far corners are often off-frame; the UI allows 25% padding."""
    _ready_session()
    monkeypatch.setattr(jobs, "enqueue_analyze", lambda sdir: None)
    monkeypatch.setattr(media, "probe_video", lambda p: {"width": 1280, "height": 720})
    ok = {**GOOD, "corners_px": [[-300, -100], [1500, -100], [1700, 900], [-400, 900]]}
    assert client.post("/api/session/cal1/calibrate", json=ok).status_code == 200
