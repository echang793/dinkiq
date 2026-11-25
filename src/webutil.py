"""Serving the split frontend: content-versioned URLs, immutable caching, selective gzip.

The dashboard was a single 172 KB HTML file re-downloaded in full on every visit over
cellular: no compression, no caching headers.
"""

import hashlib
import re
from pathlib import Path

from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

# Media must never be gzipped: it wastes CPU on already-compressed bytes and breaks
# Range requests, so seeking inside a rally clip would stop working.
_NO_GZIP = re.compile(r"^/api/session/[^/]+/(clip/\d+|frame|highlights\.mp4|report\.pdf|card\.png)$")


def should_gzip(path: str) -> bool:
    return not _NO_GZIP.match(path)


class TextGZip:
    """GZipMiddleware that leaves media/binary responses alone."""

    def __init__(self, app, minimum_size: int = 500):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and should_gzip(scope["path"]):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def asset_version(static_dir: Path) -> str:
    """A short hash of every CSS/JS file's path, size and mtime: it changes exactly
    when an asset does, so URLs carrying it can be cached forever."""
    h = hashlib.sha1()
    for f in sorted(list(static_dir.glob("css/**/*.css")) + list(static_dir.glob("js/**/*.js"))):
        st = f.stat()
        h.update(f"{f.relative_to(static_dir)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


class VersionedStatic(StaticFiles):
    """Immutable for a year when the URL carries ?v=<asset_version>; otherwise
    revalidated every time (StaticFiles already sends ETag/Last-Modified)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return resp
