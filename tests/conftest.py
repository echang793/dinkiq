"""Shared pytest fixtures.

Two guarantees for every test:

* It never touches the developer's real data/ directory. DINKIQ_DATA_DIR is
  pointed at a throwaway temp dir *before* any module is imported (pipeline
  reads it once at import; server derives UPLOADS, TRASH and
  LAST_CALIBRATION_PATH from it), and the dir is wiped around each test so
  state can't leak between them. Several tests create, delete and empty
  sessions and the saved calibration -- against real data they destroyed it.
* It isn't blocked by (or dependent on) a local DINKIQ_PASSWORD/.env --
  server.py's Basic Auth middleware reads its module globals at request time,
  so patching them here disables auth regardless of what .env contains.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TEST_DATA = Path(tempfile.mkdtemp(prefix="dinkiq-test-data-"))
os.environ["DINKIQ_DATA_DIR"] = str(_TEST_DATA)  # unconditional: never inherit a real one
atexit.register(shutil.rmtree, _TEST_DATA, ignore_errors=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _reset_test_data():
    for child in _TEST_DATA.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    (_TEST_DATA / "sessions").mkdir()
    (_TEST_DATA / "uploads").mkdir()


@pytest.fixture(autouse=True)
def _isolated_data_dir():
    _reset_test_data()
    yield
    _reset_test_data()


@pytest.fixture(autouse=True)
def _disable_auth_for_tests(monkeypatch):
    import server
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD", None, raising=False)
    monkeypatch.setattr(server, "_DINKIQ_PASSWORD_BYTES", None, raising=False)
