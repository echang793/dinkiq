"""Paddle-hit detection from audio: scale-free thresholds and accurate timestamps.

onset_detect(delta=0.35) ran on an envelope normalised to its session maximum, so
the threshold was "35% of the loudest transient in the file": one dropped paddle,
fence bang or adjacent-court smash set the scale and suppressed every ordinary
hit; a quiet recording and a loud one behaved differently. Timestamps were the
onset frame (a 512-sample hop, ~23 ms), not the pop itself.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from events import detect_hits

SR = 22050


def _wav(tmp_path, hits, dur=20.0, floor=0.002, gain=1.0, seed=0, name="a.wav"):
    """hits: [(time_s, amplitude)]. Pops are 20 ms broadband bursts with a sharp attack."""
    import soundfile as sf
    rng = np.random.default_rng(seed)
    y = rng.normal(0, floor, int(SR * dur))
    n = int(0.02 * SR)
    for t, amp in hits:
        i = int(round(t * SR))
        y[i:i + n] += rng.normal(0, amp, n) * np.exp(-np.linspace(0, 8, n))
    path = tmp_path / name
    sf.write(path, np.clip(y * gain, -1, 1), SR)
    return path


def _found(hits, t, tol=0.06):
    return len(hits) and float(np.min(np.abs(hits - t))) <= tol


REAL = [2.0, 3.1, 4.3, 5.2, 9.0, 10.1, 11.4, 15.0, 16.2]


def test_one_very_loud_bang_does_not_suppress_the_ordinary_hits(tmp_path):
    wav = _wav(tmp_path, [(t, 0.5) for t in REAL] + [(7.0, 8.0)])     # a 16x louder bang
    hits = detect_hits(wav)
    missed = [t for t in REAL if not _found(hits, t)]
    assert not missed, f"hits suppressed by the loud bang: {missed} (found {np.round(hits, 2).tolist()})"


def test_a_quiet_recording_finds_the_same_hits(tmp_path):
    loud = detect_hits(_wav(tmp_path, [(t, 0.5) for t in REAL], name="loud.wav"))
    quiet = detect_hits(_wav(tmp_path, [(t, 0.5) for t in REAL], gain=0.05, name="quiet.wav"))
    assert [t for t in REAL if not _found(loud, t)] == []
    assert [t for t in REAL if not _found(quiet, t)] == [], np.round(quiet, 2).tolist()


def test_hits_of_very_different_strength_are_all_found(tmp_path):
    amps = [0.15, 0.6, 0.3, 1.0, 0.2, 0.45, 0.8, 0.25, 0.5]
    hits = detect_hits(_wav(tmp_path, list(zip(REAL, amps))))
    missed = [t for t, a in zip(REAL, amps) if not _found(hits, t)]
    assert not missed, f"missed {missed}"


def test_timestamps_are_the_pop_not_the_analysis_frame(tmp_path):
    """A pop at an off-grid time must be reported within ~12 ms (the old code lagged by
    up to a 23 ms hop)."""
    times = [2.003, 3.117, 4.331, 5.249, 9.011, 10.173, 11.397, 15.021, 16.256]
    hits = detect_hits(_wav(tmp_path, [(t, 0.5) for t in times]))
    errs = [float(np.min(np.abs(hits - t))) for t in times]
    assert max(errs) <= 0.012, np.round(np.array(errs) * 1000, 1).tolist()


def test_pure_noise_produces_no_phantom_hits(tmp_path):
    """A scale-free threshold divides by the background level -- on a file that is ONLY
    background it must not amplify it into hits."""
    assert len(detect_hits(_wav(tmp_path, [], floor=0.05))) == 0
    assert len(detect_hits(_wav(tmp_path, [], floor=0.0005, name="b.wav"))) == 0


def test_digital_silence_is_not_a_crash_or_a_hit(tmp_path):
    import soundfile as sf
    p = tmp_path / "silence.wav"
    sf.write(p, np.zeros(SR * 5), SR)
    assert len(detect_hits(p)) == 0


def test_a_steady_loud_hum_does_not_hide_or_fake_hits(tmp_path):
    import soundfile as sf
    rng = np.random.default_rng(3)
    t = np.arange(SR * 20) / SR
    y = 0.2 * np.sin(2 * np.pi * 2500 * t) * (0.7 + 0.3 * np.sin(2 * np.pi * 0.5 * t)) + rng.normal(0, 0.01, len(t))
    n = int(0.02 * SR)
    for h in REAL:
        i = int(h * SR)
        y[i:i + n] += rng.normal(0, 0.9, n) * np.exp(-np.linspace(0, 8, n))
    p = tmp_path / "hum.wav"
    sf.write(p, np.clip(y, -1, 1), SR)
    hits = detect_hits(p)
    assert [h for h in REAL if not _found(hits, h)] == []
    assert len([h for h in hits if not any(abs(h - r) < 0.1 for r in REAL)]) <= 1


@pytest.mark.parametrize("gap", [0.30, 0.45])
def test_two_hits_close_together_are_both_counted(tmp_path, gap):
    """Fast exchanges at the net: 0.3 s apart is a real dink-volley rally."""
    hits = detect_hits(_wav(tmp_path, [(3.0, 0.5), (3.0 + gap, 0.5), (8.0, 0.5)]))
    assert _found(hits, 3.0) and _found(hits, 3.0 + gap, tol=0.05)
