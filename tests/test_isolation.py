"""The suite must never read or write the developer's real data/ directory.

Several tests create, delete and wipe sessions, trash, uploads and the saved
calibration. Pointed at the real data dir they destroy user data (they have
deleted data/last_calibration.json) and make results depend on whatever
sessions happen to exist.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline
import server

REAL_DATA = (Path(__file__).resolve().parent.parent / "data").resolve()


def _inside_real_data(p: Path) -> bool:
    p = Path(p).resolve()
    return p == REAL_DATA or REAL_DATA in p.parents


def test_session_paths_do_not_point_at_real_data():
    for name, p in {
        "pipeline.SESSIONS": pipeline.SESSIONS,
        "server.SESSIONS": server.SESSIONS,
        "server.TRASH": server.TRASH,
        "server.UPLOADS": server.UPLOADS,
        "server.LAST_CALIBRATION_PATH": server.LAST_CALIBRATION_PATH,
    }.items():
        assert not _inside_real_data(p), f"{name} -> {p} is inside the real data dir"


def test_each_test_starts_with_empty_sessions():
    leftovers = [p.name for p in pipeline.SESSIONS.iterdir()] if pipeline.SESSIONS.exists() else []
    assert leftovers == [], f"state leaked from another test: {leftovers}"


def test_leaked_state_is_cleared_between_tests_part1():
    (pipeline.SESSIONS / "leak_probe").mkdir(parents=True, exist_ok=True)
    server.LAST_CALIBRATION_PATH.write_text("{}")


def test_leaked_state_is_cleared_between_tests_part2():
    assert not (pipeline.SESSIONS / "leak_probe").exists()
    assert not server.LAST_CALIBRATION_PATH.exists()
