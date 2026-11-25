'use strict';
/* Court geometry for the calibration screen. Mirrors src/court.py: x across 0..20 ft, y along 0..44 ft,
   net at 22, kitchen lines at 15 and 29. The server fits the real homography from the taps; this fits
   the same thing in the browser so the user can SEE the fitted court (and which tap is off) before
   spending minutes on an analysis. Pure functions, no DOM: unit-tested under node. */

const COURT_W = 20, COURT_L = 44;
const CAL_CORNER_TARGETS = [[0, 0], [COURT_W, 0], [COURT_W, COURT_L], [0, COURT_L]];   // far-L, far-R, near-R, near-L
const CAL_KITCHEN_TARGETS = [[0, 15], [COURT_W, 15], [COURT_W, 29], [0, 29]];
// every line of the court, as court-space segments
const COURT_LINES = [
  [[0, 0], [COURT_W, 0]], [[COURT_W, 0], [COURT_W, COURT_L]], [[COURT_W, COURT_L], [0, COURT_L]], [[0, COURT_L], [0, 0]],
  [[0, 15], [COURT_W, 15]], [[0, 29], [COURT_W, 29]],            // kitchen lines
  [[0, 22], [COURT_W, 22]],                                      // net
  [[10, 0], [10, 15]], [[10, 29], [10, COURT_L]],                // centre lines
];

// Gaussian elimination with partial pivoting; null when (near-)singular
function solveLinear(A, b) {
  const n = b.length, M = A.map((row, i) => row.concat([b[i]]));
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (Math.abs(M[p][c]) < 1e-10) return null;
    [M[c], M[p]] = [M[p], M[c]];
    for (let r = c + 1; r < n; r++) {
      const f = M[r][c] / M[c][c];
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  const x = new Array(n).fill(0);
  for (let r = n - 1; r >= 0; r--) {
    let s = M[r][n];
    for (let k = r + 1; k < n; k++) s -= M[r][k] * x[k];
    x[r] = s / M[r][r];
  }
  return x;
}

// pairs: [[courtX, courtY], [px, py]] (>= 4). Least-squares court->image homography, h33 = 1
// (8 numbers). `scale` is the image width, only used to keep the numbers well conditioned.
function fitHomography(pairs, scale) {
  if (!pairs || pairs.length < 4) return null;
  const A = [], b = [];
  for (const [[cx, cy], [px, py]] of pairs) {
    const X = cx / COURT_L, Y = cy / COURT_L, u = px / scale, v = py / scale;
    A.push([X, Y, 1, 0, 0, 0, -u * X, -u * Y]); b.push(u);
    A.push([0, 0, 0, X, Y, 1, -v * X, -v * Y]); b.push(v);
  }
  const AtA = Array.from({length: 8}, () => new Array(8).fill(0)), Atb = new Array(8).fill(0);
  for (let i = 0; i < A.length; i++) for (let r = 0; r < 8; r++) {
    Atb[r] += A[i][r] * b[i];
    for (let c = 0; c < 8; c++) AtA[r][c] += A[i][r] * A[i][c];
  }
  const h = solveLinear(AtA, Atb);
  if (!h || h.some(v => !isFinite(v))) return null;
  return h.concat([scale]);            // 9th entry carries the scale back out
}

function courtToImage(H, x, y) {
  const s = H[8], X = x / COURT_L, Y = y / COURT_L, w = H[6] * X + H[7] * Y + 1;
  return [(H[0] * X + H[1] * Y + H[2]) / w * s, (H[3] * X + H[4] * Y + H[5]) / w * s];
}
// how far each tap is from where the fitted court says that point should be (image px)
function reprojectionErrors(H, pairs) {
  return pairs.map(([c, p]) => { const q = courtToImage(H, c[0], c[1]); return Math.hypot(q[0] - p[0], q[1] - p[1]); });
}
function worstIndex(errs) { return errs.reduce((bi, e, i) => e > errs[bi] ? i : bi, 0); }

if (typeof module !== 'undefined') module.exports = {CAL_CORNER_TARGETS, CAL_KITCHEN_TARGETS, COURT_LINES, fitHomography, courtToImage, reprojectionErrors, worstIndex};
