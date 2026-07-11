"""Event detection tests: synthetic click audio with known hit times."""

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from events import (attribute_hits, detect_hits, detect_swings, rally_metrics,
                    segment_rallies, wrist_speed)

SR = 22050
# two rallies: hits at 1,2,3s then gap, hits at 10,11s
HIT_TIMES = [1.0, 2.0, 3.0, 10.0, 11.0]


def make_click_wav(path: Path, hit_times: list[float], dur: float = 14.0) -> None:
    import soundfile as sf
    y = np.random.default_rng(0).normal(0, 0.002, int(SR * dur))  # noise floor
    for t in hit_times:
        i = int(t * SR)
        n = int(0.02 * SR)  # 20 ms broadband pop
        y[i:i + n] += np.random.default_rng(1).normal(0, 0.6, n) * np.exp(-np.linspace(0, 8, n))
    sf.write(path, y, SR)


def make_noisy_wav(path: Path, hit_times: list[float], dur: float = 14.0) -> None:
    """Clicks buried in speech-band tone, music-ish hum, and wind noise."""
    import soundfile as sf
    rng = np.random.default_rng(0)
    t = np.arange(int(SR * dur)) / SR
    y = (0.15 * np.sin(2 * np.pi * 300 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))  # "speech"
         + 0.12 * np.sin(2 * np.pi * 90 * t)                                            # bass hum
         + rng.normal(0, 0.03, len(t)))                                                 # wind
    for h in hit_times:
        i = int(h * SR)
        n = int(0.02 * SR)
        y[i:i + n] += rng.normal(0, 0.7, n) * np.exp(-np.linspace(0, 8, n))
    sf.write(path, y, SR)


def test_hits_survive_noise_speech_music():
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "noisy.wav"
        make_noisy_wav(wav, HIT_TIMES)
        hits = detect_hits(wav)
        for t in HIT_TIMES:  # every true hit still found
            assert np.min(np.abs(hits - t)) < 0.06, (t, hits)
        # no phantom hits from speech/music/wind (allow tiny slack)
        extras = [h for h in hits if np.min(np.abs(np.array(HIT_TIMES) - h)) > 0.1]
        assert len(extras) <= 1, extras


def test_detect_hits_and_rallies():
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        make_click_wav(wav, HIT_TIMES)
        hits = detect_hits(wav)
        # every true hit found within 60 ms
        for t in HIT_TIMES:
            assert np.min(np.abs(hits - t)) < 0.06, (t, hits)
        rallies = segment_rallies(hits)
        assert len(rallies) == 2, rallies
        assert rallies[0]["hits"] >= 3 and rallies[1]["hits"] >= 2, rallies


def test_swing_detection_and_attribution():
    fps = 30.0
    n = 120  # 4 s
    frames = np.arange(n)
    lw = np.full((n, 2), 100.0)
    # swing at t=2.0s: wrist whips 40 px/frame for 6 frames (1200 px/s)
    for k in range(6):
        lw[60 + k] = [100 + 40 * k, 100]
    sub = pd.DataFrame({
        "frame": frames, "lwx": lw[:, 0], "lwy": lw[:, 1],
        "rwx": np.zeros(n), "rwy": np.zeros(n),  # right wrist undetected
    })
    swings = detect_swings(wrist_speed(sub, fps))
    assert len(swings) >= 1 and abs(swings[0] - 2.0) < 0.3, swings

    hits = np.array([2.05, 8.0])
    mine = attribute_hits(hits, swings)
    assert mine[0] and not mine[1], mine


def test_rally_metrics_empty():
    m = rally_metrics([], np.array([]), np.array([]), 60.0)
    assert m["rally_count"] == 0


if __name__ == "__main__":
    for fn in [test_hits_survive_noise_speech_music, test_detect_hits_and_rallies,
               test_swing_detection_and_attribution, test_rally_metrics_empty]:
        fn()
        print(f"ok {fn.__name__}")
