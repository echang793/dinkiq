"""Installable on a phone: manifest, icons, service worker, offline page.

The service worker caches the app shell ONLY. Caching /api/* would show stale (or someone's
old) results as if they were current, which is exactly what the offline banner exists to avoid.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import server

client = TestClient(server.app)
STATIC = Path(__file__).resolve().parent.parent / "static"


def test_manifest_is_installable():
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and "manifest+json" in r.headers["content-type"]
    m = r.json()
    assert m["name"] == "DinkIQ" and m["display"] == "standalone"
    assert m["start_url"].startswith(".") and m["scope"].startswith(".")      # relative: survives a /app/dinkiq/ mount
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any("maskable" in i.get("purpose", "") for i in m["icons"])
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", m["theme_color"]) and re.fullmatch(r"#[0-9a-fA-F]{6}", m["background_color"])


def test_every_manifest_icon_is_served_as_a_png_of_the_right_size():
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        r = client.get("/" + icon["src"].lstrip("./"))
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", icon
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(r.content[16:20], "big") == w and int.from_bytes(r.content[20:24], "big") == h


def test_service_worker_is_served_fresh_and_never_touches_the_api():
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "no-cache" in r.headers["cache-control"]          # a stale worker can't be fixed once installed
    src = r.text
    assert "/api/" in src and "return;" in src                # the api branch exists and bypasses the cache
    assert "event.request.method" in src or "req.method" in src


def test_offline_page_is_served_and_tells_the_truth():
    r = client.get("/offline")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "can't reach" in r.text.lower() and "no sample" not in r.text.lower()


def test_page_links_everything_the_phone_needs():
    html = client.get("/").text
    assert 'rel="manifest"' in html and 'crossorigin="use-credentials"' in html     # the gate is basic auth
    assert 'name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "viewport-fit=cover" in html                                              # safe-area insets
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html    # self-hosted now
    assert "serviceWorker" in (STATIC / "js" / "boot.js").read_text()


def test_fonts_are_served_locally_and_cached_forever():
    for name in ("archivo.woff2", "archivo-italic.woff2", "archivo-black.woff2"):
        r = client.get(f"/static/fonts/{name}?v=abc")
        assert r.status_code == 200 and "font/woff2" in r.headers["content-type"]
        assert "immutable" in r.headers["cache-control"]
    assert "@font-face" in (STATIC / "css" / "app.css").read_text()
    assert json.loads(client.get("/manifest.webmanifest").text)["icons"]
