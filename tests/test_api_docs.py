"""docs/API.md must describe exactly the routes the app serves.

The doc had drifted badly: ~20 routes undocumented, "delete is permanent" (it is
a 7-day trash), the multipart upload documented long after it was the chunked
one. Each endpoint has one `### `METHOD /path`` heading; this fails when a route
is added/removed/renamed without the doc (or the doc invents one).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import server

DOC = Path(__file__).resolve().parent.parent / "docs" / "API.md"
HEADING = re.compile(r"^### `(GET|POST|PUT|PATCH|DELETE) (/[^`\s]*)`", re.M)


def _served():
    ops = set()
    for path, methods in server.app.openapi()["paths"].items():
        if path.startswith("/api/"):
            ops |= {f"{m.upper()} {path[len('/api'):]}" for m in methods}
    return ops


def _documented():
    return {f"{m} {p}" for m, p in HEADING.findall(DOC.read_text())}


def test_every_served_api_route_is_documented():
    missing = sorted(_served() - _documented())
    assert not missing, f"routes with no docs/API.md heading: {missing}"


def test_every_documented_route_exists():
    invented = sorted(_documented() - _served())
    assert not invented, f"docs/API.md documents routes the app doesn't serve: {invented}"


def test_no_route_is_documented_twice():
    found = [f"{m} {p}" for m, p in HEADING.findall(DOC.read_text())]
    assert len(found) == len(set(found))


def test_removed_multipart_endpoints_are_not_advertised():
    text = DOC.read_text()
    assert "multipart" not in text.lower().replace("no multipart", "").replace("not multipart", "")
