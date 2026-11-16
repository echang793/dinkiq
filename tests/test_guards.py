"""Restart recovery and request guards.

Before: the analysis queue lives only in memory, so a restart mid-job left the
session in "queued"/"tracking" forever -- and both recalibrate and reprocess
refuse sessions that are still processing, so the only way out was deleting it.
Also: delete/reupload ignored running jobs, reupload destroyed the original
before checking the replacement, concurrent chunk PUTs could duplicate bytes,
ids like ".trash" were accepted, and user text could land in a CSV formula cell.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx2 as httpx  # starlette 1.x test client is built on httpx2
from fastapi.testclient import TestClient

import pipeline
import server

client = TestClient(server.app)


def _mk(sid, status=None, calibrated=False, raw=None, meta=None, age_s=0):
    d = pipeline.SESSIONS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta if meta is not None else {"filename": f"{sid}.mp4"}))
    if status is not None:
        sf = d / "status.json"
        sf.write_text(json.dumps(status))
        if age_s:
            t = time.time() - age_s
            os.utime(sf, (t, t))
    if calibrated:
        (d / "calibration.json").write_text(json.dumps(
            {"corners_px": [[0, 0], [1, 0], [1, 1], [0, 1]], "self_px": [1, 1]}))
    if raw:
        (d / raw).write_bytes(b"x")
    return d


def _status(d):
    return json.loads((d / "status.json").read_text())


# ── restart recovery ────────────────────────────────────────────────────

@pytest.fixture
def spy(monkeypatch):
    calls = {"analyze": [], "ingest": []}
    monkeypatch.setattr(pipeline, "enqueue_analyze", lambda d: calls["analyze"].append(d.name))
    monkeypatch.setattr(pipeline, "_spawn_ingest", lambda d, raw: calls["ingest"].append((d.name, raw.name)))
    return calls


def test_interrupted_analysis_with_calibration_is_requeued(spy):
    _mk("q1", {"stage": "queued", "state": "queued"}, calibrated=True)
    _mk("t1", {"stage": "tracking", "state": "running", "progress": 0.4}, calibrated=True)
    _mk("r1", {"stage": "rating", "state": "running"}, calibrated=True)
    out = pipeline.recover_interrupted()
    assert sorted(spy["analyze"]) == ["q1", "r1", "t1"]
    assert sorted(out["requeued"]) == ["q1", "r1", "t1"]


def test_requeue_preserves_fifo_order_oldest_first(spy):
    _mk("newer", {"stage": "queued", "state": "queued"}, calibrated=True, age_s=10)
    _mk("oldest", {"stage": "queued", "state": "queued"}, calibrated=True, age_s=500)
    _mk("middle", {"stage": "tracking", "state": "running"}, calibrated=True, age_s=100)
    pipeline.recover_interrupted()
    assert spy["analyze"] == ["oldest", "middle", "newer"]


def test_interrupted_analysis_without_calibration_becomes_a_recoverable_error(spy):
    d = _mk("nocal", {"stage": "tracking", "state": "running"})
    out = pipeline.recover_interrupted()
    assert spy["analyze"] == [] and out["failed"] == ["nocal"]
    st = _status(d)
    assert st["state"] == "error" and "restart" in st["error"].lower()
    assert server._calibratable(pipeline.get_status(d))  # the user can now recalibrate


def test_interrupted_ingest_is_restarted_from_the_kept_raw_file(spy):
    _mk("ing1", {"stage": "ingest", "state": "running"}, raw="raw.mov")
    out = pipeline.recover_interrupted()
    assert spy["ingest"] == [("ing1", "raw.mov")] and out["reingested"] == ["ing1"]


def test_interrupted_ingest_without_raw_becomes_an_error(spy):
    d = _mk("ing2", {"stage": "ingest", "state": "running"})
    out = pipeline.recover_interrupted()
    assert spy["ingest"] == [] and out["failed"] == ["ing2"]
    assert "re-upload" in _status(d)["error"].lower()


def test_settled_sessions_and_trash_are_left_alone(spy):
    for sid, st in {"done1": {"stage": "done", "state": "done"},
                    "err1": {"stage": "tracking", "state": "error", "error": "x"},
                    "idle1": {"stage": "ingest", "state": "done"}}.items():
        _mk(sid, st, calibrated=True)
    _mk("nostatus")
    (pipeline.SESSIONS / ".trash" / "gone1").mkdir(parents=True)
    (pipeline.SESSIONS / ".trash" / "gone1" / "status.json").write_text(
        json.dumps({"stage": "tracking", "state": "running"}))
    out = pipeline.recover_interrupted()
    assert spy == {"analyze": [], "ingest": []}
    assert not any(out.values())


def test_unreadable_status_is_not_touched_by_recovery(spy):
    d = _mk("corrupt")
    (d / "status.json").write_text('{"stage": "trac')
    pipeline.recover_interrupted()
    assert spy == {"analyze": [], "ingest": []}
    assert (d / "status.json").read_text() == '{"stage": "trac'


def test_recovery_runs_on_server_startup(monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "recover_interrupted", lambda: seen.append(1) or {})
    with TestClient(server.app):
        pass
    assert seen == [1]


def test_a_recovery_failure_does_not_stop_the_server_starting(monkeypatch):
    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(pipeline, "recover_interrupted", boom)
    with TestClient(server.app) as c:
        assert c.get("/api/sessions").status_code == 200


# ── delete / replace refuse a busy session ─────────────────────────────

@pytest.mark.parametrize("state", ["running", "queued"])
def test_delete_refuses_a_session_that_is_processing(state):
    d = _mk("busy1", {"stage": "tracking", "state": state})
    r = client.post("/api/sessions/delete", json={"ids": ["busy1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == [] and "processing" in body["errors"][0]["error"]
    assert d.exists()


def test_bulk_delete_skips_only_the_busy_session():
    busy = _mk("busy2", {"stage": "tracking", "state": "running"})
    idle = _mk("idle2", {"stage": "done", "state": "done"})
    body = client.post("/api/sessions/delete", json={"ids": ["busy2", "idle2"]}).json()
    assert body["deleted"] == ["idle2"] and busy.exists() and not idle.exists()


# ── replacing a video validates BEFORE destroying the original ──────────
# (chunked path: init{session_id} -> PUT chunks -> finish{session_id})

def _arm_reupload(monkeypatch, readable=True):
    monkeypatch.setattr(pipeline, "ingest", lambda sdir, raw: None)
    monkeypatch.setattr(pipeline, "probe_video", lambda p: (
        {"codec": "h264", "width": 1280, "height": 720, "fps": 30.0, "frames": 300, "duration": 10.0}
        if readable else {"codec": None, "width": 0, "height": 0, "fps": 0.0, "frames": 0, "duration": 0.0}))
    monkeypatch.setattr(server.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: None})())


def _replace(sid, filename="new.mp4", data=b"fresh"):
    """Run the whole replacement flow; return the first non-200 response, else finish's."""
    init = client.post("/api/upload/init", json={"filename": filename, "session_id": sid})
    if init.status_code != 200:
        return init
    uid = init.json()["upload_id"]
    put = client.put(f"/api/upload/{uid}?offset=0", content=data)
    if put.status_code != 200:
        return put
    return client.post(f"/api/upload/{uid}/finish", json={"filename": filename, "session_id": sid})


def test_replacing_a_session_that_is_processing_is_refused_up_front():
    d = _mk("busy3", {"stage": "tracking", "state": "running"}, raw="raw.mp4")
    r = client.post("/api/upload/init", json={"filename": "n.mp4", "session_id": "busy3"})
    assert r.status_code == 409  # refused at init, before any bytes are sent
    assert (d / "raw.mp4").read_bytes() == b"x"


def test_session_that_became_busy_after_init_is_still_refused_at_finish(monkeypatch):
    _arm_reupload(monkeypatch)
    d = _mk("busy4", {"stage": "done", "state": "done"}, raw="raw.mp4")
    uid = client.post("/api/upload/init", json={"filename": "n.mp4", "session_id": "busy4"}).json()["upload_id"]
    client.put(f"/api/upload/{uid}?offset=0", content=b"new")
    (d / "status.json").write_text(json.dumps({"stage": "tracking", "state": "running"}))
    r = client.post(f"/api/upload/{uid}/finish", json={"filename": "n.mp4", "session_id": "busy4"})
    assert r.status_code == 409 and (d / "raw.mp4").read_bytes() == b"x"


def test_replacing_an_unknown_or_malformed_session_is_rejected():
    assert client.post("/api/upload/init", json={"filename": "n.mp4", "session_id": "nope"}).status_code == 404
    assert client.post("/api/upload/init", json={"filename": "n.mp4", "session_id": ".trash"}).status_code == 400


def test_replacement_of_an_unreadable_file_keeps_the_original(monkeypatch):
    _arm_reupload(monkeypatch, readable=False)
    d = _mk("re1", {"stage": "done", "state": "done"}, raw="raw.mp4")
    (d / "video.mp4").write_bytes(b"good analysed video")
    (d / "metrics.json").write_text("{}")
    r = _replace("re1", "junk.mp4", b"not a video")
    assert r.status_code == 400 and "original was kept" in r.json()["detail"]
    assert (d / "raw.mp4").read_bytes() == b"x"
    assert (d / "video.mp4").exists() and (d / "metrics.json").exists()  # analysis not wiped
    assert not list(d.glob("incoming*"))  # no stray temp file


def test_replacement_with_bad_extension_is_rejected_at_init(monkeypatch):
    _arm_reupload(monkeypatch)
    d = _mk("re2", {"stage": "done", "state": "done"}, raw="raw.mp4")
    assert _replace("re2", "notes.txt", b"hi").status_code == 400
    assert (d / "raw.mp4").read_bytes() == b"x"


def test_replacement_over_the_size_limit_keeps_the_original(monkeypatch):
    _arm_reupload(monkeypatch)
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 5)
    d = _mk("re3", {"stage": "done", "state": "done"}, raw="raw.mp4")
    assert _replace("re3", "big.mp4", b"0123456789").status_code == 413
    assert (d / "raw.mp4").read_bytes() == b"x"


def test_replacement_with_a_good_file_swaps_it_in_and_clears_derived(monkeypatch):
    _arm_reupload(monkeypatch)
    d = _mk("re4", {"stage": "done", "state": "done"}, raw="raw.mov", calibrated=True)
    (d / "video.mp4").write_bytes(b"old")
    (d / "metrics.json").write_text("{}")
    r = _replace("re4", "new.mp4", b"fresh")
    assert r.status_code == 200 and r.json()["session_id"] == "re4"
    assert (d / "raw.mp4").read_bytes() == b"fresh" and not (d / "raw.mov").exists()
    assert not (d / "video.mp4").exists() and not (d / "metrics.json").exists()
    assert not (d / "calibration.json").exists()
    assert json.loads((d / "meta.json").read_text())["filename"] == "new.mp4"
    assert _status(d) == {"stage": "ingest", "state": "running", "overall": 0.0}  # not the old "done"


def test_the_unbounded_multipart_endpoints_are_gone():
    """python-multipart spools the whole body to disk before any handler runs,
    so these ignored the 2 GB limit; the chunked path replaces both."""
    _mk("mp1", {"stage": "done", "state": "done"})
    files = {"file": ("c.mp4", b"x", "video/mp4")}
    assert client.post("/api/upload", files=files).status_code in (404, 405)
    assert client.post("/api/session/mp1/reupload", files=files).status_code in (404, 405)


# ── concurrent chunk PUTs can't duplicate bytes ─────────────────────────

def test_a_retry_racing_the_original_chunk_cannot_corrupt_the_upload():
    """Phone times out and retries chunk 0 while the server is still receiving
    the first attempt. Both used to pass the offset check and both appended."""
    uid = client.post("/api/upload/init", json={"filename": "c.mp4"}).json()["upload_id"]

    async def run():
        release = asyncio.Event()

        async def slow_body():
            yield b"AAAA"
            await release.wait()
            yield b"BBBB"

        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8100") as c:
            first = asyncio.create_task(c.put(f"/api/upload/{uid}?offset=0", content=slow_body()))
            await asyncio.sleep(0.2)  # first attempt is now mid-stream
            retry = asyncio.create_task(c.put(f"/api/upload/{uid}?offset=0", content=b"XXXXXXXX"))
            await asyncio.sleep(0.2)
            release.set()
            return await asyncio.gather(first, retry)

    first, retry = asyncio.run(run())
    assert first.status_code == 200 and first.json()["offset"] == 8
    assert retry.status_code == 409
    assert server._upload_part(uid).read_bytes() == b"AAAABBBB"


# ── session id shape ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [".trash", ".hidden", "-dash", "a b", "a" * 65, "x.y"])
def test_session_ids_must_be_plain_names(bad):
    with pytest.raises(ValueError):
        pipeline.session_dir(bad)


@pytest.mark.parametrize("good", ["abc123def456", "kitchen_test", "hl_test_top_n", "A-b_9", "a" * 64])
def test_normal_session_ids_are_accepted(good):
    assert pipeline.session_dir(good).name == good


def test_trash_dir_is_not_addressable_as_a_session():
    (pipeline.SESSIONS / ".trash").mkdir(parents=True, exist_ok=True)
    assert client.get("/api/session/.trash").status_code == 400
    assert client.patch("/api/session/.trash/meta", json={"label": "x"}).status_code == 400
    body = client.post("/api/sessions/delete", json={"ids": [".trash"]}).json()
    assert body["deleted"] == [] and body["errors"][0]["error"] == "bad session id"


# ── played_at must be a real date ───────────────────────────────────────

@pytest.mark.parametrize("bad", ["<i>x", "2026-13-45", "2026-1-5", "yesterday", "26-01-01", "2026/01/05", " 2026-01-05"])
def test_played_at_rejects_non_dates(bad):
    _mk("pa1")
    assert client.patch("/api/session/pa1/meta", json={"played_at": bad}).status_code == 422


def test_played_at_empty_string_clears_the_date():
    _mk("pa3", meta={"filename": "f.mp4", "played_at": "2026-01-01"})
    r = client.patch("/api/session/pa3/meta", json={"played_at": ""})
    assert r.status_code == 200 and r.json()["played_at"] == ""


def test_played_at_accepts_a_real_date():
    _mk("pa2")
    r = client.patch("/api/session/pa2/meta", json={"played_at": "2026-02-28"})
    assert r.status_code == 200 and r.json()["played_at"] == "2026-02-28"


# ── CSV export can't carry a spreadsheet formula ────────────────────────

def test_csv_export_neutralises_formula_cells():
    d = _mk("csv1", {"stage": "done", "state": "done"},
            meta={"filename": "f.mp4", "label": "=HYPERLINK(\"http://evil\",\"x\")",
                  "opponent": "+cmd|' /C calc'!A0", "player": "@SUM(A1)"})
    (d / "metrics.json").write_text(json.dumps({"zone_pct": {"kitchen": 40.0}}))
    text = client.get("/api/sessions/export.csv").text
    import csv
    import io
    row = next(csv.DictReader(io.StringIO(text)))
    for field in ("label", "opponent", "player"):
        assert row[field][0] == "'", (field, row[field])
    assert row["kitchen_pct"] == "40.0"  # real numbers untouched


def test_csv_export_leaves_ordinary_text_alone():
    d = _mk("csv2", {"stage": "done", "state": "done"},
            meta={"filename": "f.mp4", "label": "Sat league - wk 4", "opponent": "Sam"})
    (d / "metrics.json").write_text("{}")
    import csv
    import io
    row = next(csv.DictReader(io.StringIO(client.get("/api/sessions/export.csv").text)))
    assert row["label"] == "Sat league - wk 4" and row["opponent"] == "Sam"
