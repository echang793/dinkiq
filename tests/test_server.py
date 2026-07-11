"""Server endpoint tests: bulk delete safety (path traversal, missing ids)."""

import sys
from pathlib import Path

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
    r = client.post("/api/sessions/delete",
                    json={"ids": ["testdel_aaa", "testdel_bbb"]})
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["deleted"]) == ["testdel_aaa", "testdel_bbb"]
    assert not a.exists() and not b.exists()


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


if __name__ == "__main__":
    for fn in [test_bulk_delete, test_delete_rejects_traversal_and_missing,
               test_delete_empty_ids_rejected]:
        fn()
        print(f"ok {fn.__name__}")
