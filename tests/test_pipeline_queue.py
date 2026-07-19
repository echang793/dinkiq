"""Analysis queue position tracking + friendly ingest error mapping."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline


def test_queue_position_reflects_order(monkeypatch, tmp_path):
    release = threading.Event()

    def fake_analyze(sdir):
        release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "analyze", fake_analyze)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    try:
        pipeline.enqueue_analyze(a)
        pipeline.enqueue_analyze(b)
        time.sleep(0.1)  # let the worker pick up `a`
        assert pipeline.queue_position(a) == 0   # running
        assert pipeline.queue_position(b) == 1   # waiting behind a
    finally:
        release.set()
        pipeline._jobs.join()


def test_queue_position_none_when_not_queued(tmp_path):
    assert pipeline.queue_position(tmp_path / "never_queued") is None


def test_friendly_error_maps_known_ffmpeg_failures():
    assert "corrupted" in pipeline._friendly_error("Invalid data found when processing input")
    assert "readable video track" in pipeline._friendly_error("no video stream found")
    assert "truncated" in pipeline._friendly_error("moov atom not found")
    assert pipeline._friendly_error("some unrelated error") == "some unrelated error"


def test_notify_webhook_noop_without_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DINKIQ_WEBHOOK_URL", raising=False)
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
    pipeline.notify_webhook(tmp_path, ok=True)
    assert calls == []


def test_notify_webhook_posts_when_url_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    (tmp_path / "meta.json").write_text('{"label": "Test Session"}')
    calls = []
    monkeypatch.setattr("requests.post", lambda url, json, timeout: calls.append((url, json, timeout)))
    pipeline.notify_webhook(tmp_path, ok=True)
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://example.invalid/hook"
    assert "Test Session" in payload["content"]
    assert "finished" in payload["content"]


def test_notify_webhook_error_message_included(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    calls = []
    monkeypatch.setattr("requests.post", lambda url, json, timeout: calls.append(json))
    pipeline.notify_webhook(tmp_path, ok=False, error="ball coverage too low")
    assert "ball coverage too low" in calls[0]["content"]


def test_notify_webhook_never_raises_on_post_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("DINKIQ_WEBHOOK_URL", "https://example.invalid/hook")
    def boom(*a, **kw): raise ConnectionError("no network")
    monkeypatch.setattr("requests.post", boom)
    pipeline.notify_webhook(tmp_path, ok=True)  # must not raise


if __name__ == "__main__":
    print("run via pytest (uses monkeypatch/tmp_path fixtures)")
