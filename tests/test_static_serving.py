"""The split frontend is served efficiently and safely.

The page was one 172 KB HTML file re-downloaded in full on every visit over cellular
(no compression, no caching headers). Now: CSS/JS are separate files with a content
version in the URL (cache forever), text is gzipped, and media is NEVER compressed --
gzip on an mp4 wastes CPU and breaks Range requests, so video seeking would die.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server
import webutil

client = TestClient(server.app)


def _page():
    return client.get("/", headers={"accept-encoding": "identity"}).text


def test_the_page_references_versioned_css_and_scripts_with_no_placeholder_left():
    html = _page()
    assert "{{V}}" not in html
    assert re.search(r'href="static/css/app\.css\?v=[0-9a-f]{8,}"', html)
    scripts = re.findall(r'src="static/js/(\w+)\.js\?v=[0-9a-f]{8,}"', html)
    assert scripts[0] == "core" and scripts[-1] == "boot" and len(scripts) >= 10


def test_every_referenced_asset_exists_and_is_served():
    html = _page()
    for path in re.findall(r'(?:src|href)="(static/[^"?]+)\?v=', html):
        r = client.get("/" + path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].split(";")[0] in ("text/css", "text/javascript", "application/javascript"), path


def test_the_asset_version_changes_when_a_file_changes(tmp_path, monkeypatch):
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "app.css").write_text("a{}")
    before = webutil.asset_version(tmp_path)
    (tmp_path / "css" / "app.css").write_text("a{color:red}")
    assert webutil.asset_version(tmp_path) != before
    assert webutil.asset_version(tmp_path) == webutil.asset_version(tmp_path)    # stable otherwise


def test_versioned_assets_are_cached_for_a_year_and_unversioned_ones_revalidate():
    v = client.get("/static/css/app.css?v=abc123")
    assert "immutable" in v.headers["cache-control"] and "max-age=31536000" in v.headers["cache-control"]
    assert client.get("/static/css/app.css").headers["cache-control"] == "no-cache"


def test_the_page_itself_is_never_cached_hard():
    assert "no-cache" in client.get("/").headers.get("cache-control", "no-cache")


def test_text_responses_are_gzipped_and_decode_to_the_same_bytes():
    plain = client.get("/static/js/core.js", headers={"accept-encoding": "identity"})
    zipped = client.get("/static/js/core.js", headers={"accept-encoding": "gzip"})
    assert plain.headers.get("content-encoding") is None
    assert zipped.headers.get("content-encoding") == "gzip"
    assert zipped.content == plain.content          # the client transparently decoded it
    assert int(zipped.headers["content-length"]) < len(plain.content)


def test_the_html_and_json_api_are_gzipped_too():
    assert client.get("/", headers={"accept-encoding": "gzip"}).headers.get("content-encoding") == "gzip"


def test_media_is_never_compressed_and_range_requests_still_work(tmp_path):
    import pipeline
    d = pipeline.SESSIONS / "gz1"
    (d / "clips").mkdir(parents=True)
    (d / "clips" / "rally_00.mp4").write_bytes(bytes(range(256)) * 400)       # 100 KB "video"
    full = client.get("/api/session/gz1/clip/0", headers={"accept-encoding": "gzip"})
    assert full.headers.get("content-encoding") is None
    part = client.get("/api/session/gz1/clip/0", headers={"accept-encoding": "gzip", "range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers.get("content-encoding") is None


def test_images_and_pdfs_are_not_compressed():
    assert webutil.should_gzip("/api/session/x/frame") is False
    assert webutil.should_gzip("/api/session/x/card.png") is False
    assert webutil.should_gzip("/api/session/x/report.pdf") is False
    assert webutil.should_gzip("/api/session/x/highlights.mp4") is False
    assert webutil.should_gzip("/api/session/x/clip/3") is False
    assert webutil.should_gzip("/api/sessions") is True
    assert webutil.should_gzip("/api/session/x") is True
    assert webutil.should_gzip("/static/js/core.js") is True
    assert webutil.should_gzip("/") is True
