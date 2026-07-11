"""Court calibration tests: known corner pixels must map to exact court coords."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from court import CORNER_TARGETS, CourtCalibration, on_court, zone_for


def test_corner_roundtrip():
    # synthetic camera: court seen as a trapezoid (far edge shorter)
    corners = [[400, 200], [880, 200], [1180, 680], [100, 680]]
    calib = CourtCalibration(corners)
    mapped = calib.to_court(np.array(corners, dtype=float))
    assert np.allclose(mapped, CORNER_TARGETS, atol=1e-3), mapped


def test_center_maps_inside_court():
    corners = [[400, 200], [880, 200], [1180, 680], [100, 680]]
    calib = CourtCalibration(corners)
    center_px = np.array([[640.0, 440.0]])
    x, y = calib.to_court(center_px)[0]
    assert 0 < x < 20 and 0 < y < 44, (x, y)


def test_zones():
    assert zone_for(22.0) == "kitchen"        # at the net
    assert zone_for(15.0) == "kitchen"        # NVZ line (7ft) + buffer
    assert zone_for(10.0) == "transition"     # 12 ft from net: no-man's land
    assert zone_for(2.0) == "baseline"
    assert zone_for(42.0) == "baseline"


def test_on_court_margin():
    assert on_court(10, 22)
    assert on_court(-2, 44)          # slightly out of bounds: still play
    assert not on_court(40, 22)      # nowhere near the court


if __name__ == "__main__":
    for fn in [test_corner_roundtrip, test_center_maps_inside_court, test_zones, test_on_court_margin]:
        fn()
        print(f"ok {fn.__name__}")
