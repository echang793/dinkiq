"""Event detection tests: synthetic click audio with known hit times."""

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from events import (attribute_hits, corroborate_hits, detect_hits, detect_swings,
                    rally_metrics, segment_rallies, wrist_speed)

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
    swings = detect_swings(wrist_speed(sub, fps, court_width_px=900.0))
    assert len(swings) >= 1 and abs(swings[0] - 2.0) < 0.3, swings

    hits = np.array([2.05, 8.0])
    mine = attribute_hits(hits, swings)
    assert mine[0] and not mine[1], mine


def test_wrist_speed_normalizes_by_court_width():
    """Same raw wrist motion, different court_width_px, must produce
    inversely-scaled normalized speed -- this is the whole fix: a raw px/s
    threshold silently under/over-detects swings depending on how tightly
    the camera frames the court, same generalization risk shots.py's
    ball-speed handling already avoids via the same normalization."""
    fps = 30.0
    lw = np.array([[100.0, 100.0], [140.0, 100.0], [180.0, 100.0],
                   [220.0, 100.0], [260.0, 100.0]])  # 40 px/frame = 1200 px/s
    sub = pd.DataFrame({"frame": np.arange(5), "lwx": lw[:, 0], "lwy": lw[:, 1],
                        "rwx": np.zeros(5), "rwy": np.zeros(5)})
    narrow_peak = wrist_speed(sub, fps, court_width_px=600.0)["speed"].max()
    wide_peak = wrist_speed(sub, fps, court_width_px=1200.0)["speed"].max()
    assert abs(narrow_peak - 2 * wide_peak) < 1e-6, (narrow_peak, wide_peak)


def test_rally_metrics_empty():
    m = rally_metrics([], np.array([]), np.array([]), 60.0)
    assert m["rally_count"] == 0


def test_corroborate_hits_drops_unswung_audio():
    # 1.0/2.0 are warmup noise (paddle taps, footsteps) with no wrist swing;
    # 10.0/11.0 are real rally hits backed by subject/opponent swings
    hits = np.array([1.0, 2.0, 10.0, 11.0])
    swings = {"subject": np.array([10.02]), "opponent1": np.array([11.03])}
    kept = corroborate_hits(hits, swings)
    assert sorted(kept.tolist()) == [10.0, 11.0], kept


def test_corroborate_hits_skips_filter_when_no_swings_tracked():
    # tracking degraded entirely (no swings at all) -> don't zero out every hit
    hits = np.array([1.0, 2.0, 3.0])
    kept = corroborate_hits(hits, {"subject": np.array([]), "opponent1": np.array([])})
    assert list(kept) == list(hits)


def test_corroborate_hits_doubles_any_of_four_players_corroborates():
    hits = np.array([1.0, 2.0, 3.0, 4.0, 99.0])
    swings = {"subject": np.array([1.02]), "partner": np.array([2.03]),
             "opponent1": np.array([3.01]), "opponent2": np.array([4.04])}
    kept = corroborate_hits(hits, swings)
    assert sorted(kept.tolist()) == [1.0, 2.0, 3.0, 4.0], kept


if __name__ == "__main__":
    for fn in [test_hits_survive_noise_speech_music, test_detect_hits_and_rallies,
               test_swing_detection_and_attribution, test_wrist_speed_normalizes_by_court_width,
               test_rally_metrics_empty,
               test_corroborate_hits_drops_unswung_audio,
               test_corroborate_hits_skips_filter_when_no_swings_tracked,
               test_corroborate_hits_doubles_any_of_four_players_corroborates]:
        fn()
        print(f"ok {fn.__name__}")
