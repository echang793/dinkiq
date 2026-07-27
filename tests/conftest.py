"""Shared pytest fixtures.

Tests must not depend on (or be blocked by) a developer's local
DINKIQ_PASSWORD/.env -- server.py's Basic Auth middleware reads its
module globals at request time, so patching them directly here disables
auth for every TestClient-based test regardless of what .env contains.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest


@pytest.fixture(autouse=True)
def _disable_auth_for_tests(monkeypatch):
    import server
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD", None, raising=False)
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD_BYTES", None, raising=False)
