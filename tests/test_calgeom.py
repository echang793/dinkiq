"""The calibration screen fits the court to the user's taps in the browser, so they can see the
fitted court before analysing and get told which tap is off. That geometry is plain JS; run it
under node (skipped where node isn't installed)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "js" / "calgeom.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(script: str):
    prog = f"const g = require({json.dumps(str(JS))}); const out = (()=>{{ {script} }})(); console.log(JSON.stringify(out));"
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# a plausible camera: a known court->image homography to generate "taps" from
H_TRUE = "[24.0, -3.0, 300.0, 1.5, 9.0, 120.0, 0.0004, 0.002]"


def pairs_js(n_kitchen: int = 4) -> str:
    return f"""
      const H = {H_TRUE}.concat([]);
      const proj = (x, y) => {{ const X = x/44, Y = y/44; const w = H[6]*X + H[7]*Y + 1;
        return [(H[0]*X + H[1]*Y + H[2])/w * 1000, (H[3]*X + H[4]*Y + H[5])/w * 1000]; }};
      const tg = g.CAL_CORNER_TARGETS.concat(g.CAL_KITCHEN_TARGETS).slice(0, 4 + {n_kitchen});
      const pairs = tg.map(t => [t, proj(t[0], t[1])]);
    """


def test_fit_recovers_a_known_camera():
    out = run(pairs_js() + """
      const fit = g.fitHomography(pairs, 1000);
      const probe = [[10, 22], [3, 40], [17, 8]];
      return probe.map(p => { const a = g.courtToImage(fit, p[0], p[1]); const b = proj(p[0], p[1]); return Math.hypot(a[0]-b[0], a[1]-b[1]); });
    """)
    assert max(out) < 1e-3          # px


def test_four_taps_are_enough_for_a_preview():
    out = run(pairs_js(0) + "const fit = g.fitHomography(pairs, 1000); return fit !== null && fit.length === 9;")
    assert out is True


def test_a_misplaced_kitchen_corner_is_named():
    out = run(pairs_js() + """
      pairs[6][1] = [pairs[6][1][0] + 45, pairs[6][1][1] - 20];      // K3 tapped ~49 px off
      const fit = g.fitHomography(pairs, 1000);
      const errs = g.reprojectionErrors(fit, pairs);
      return {worst: g.worstIndex(errs), max: Math.max(...errs)};
    """)
    assert out["worst"] == 6 and out["max"] > 15


def test_a_clean_fit_has_tiny_residuals():
    out = run(pairs_js() + "const fit = g.fitHomography(pairs, 1000); return Math.max(...g.reprojectionErrors(fit, pairs));")
    assert out < 1e-3


def test_collinear_taps_do_not_produce_a_fit():
    out = run("""
      const pairs = g.CAL_CORNER_TARGETS.map((t, i) => [t, [100 + i*50, 200]]);   // all on one line
      return g.fitHomography(pairs, 1000);
    """)
    assert out is None
