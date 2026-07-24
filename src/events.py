"""M2 event detection: paddle hits (audio), rallies, swings, shot attribution.

Paddle contact is a sharp broadband transient ("pop"). We detect audio onsets,
group them into rallies by inter-hit gaps, and attribute hits to the subject
when a wrist-speed peak (swing) lands within a small window of the hit sound.
"""

import numpy as np
import pandas as pd

MIN_HIT_GAP_S = 0.25      # two paddle hits can't be closer than this
RALLY_GAP_S = 5.0         # silence longer than this ends a rally
MIN_RALLY_HITS = 2        # single bang (dropped paddle, door) is not a rally
SWING_MATCH_S = 0.25      # swing peak within this window of a hit = subject's shot
# court-widths/s, not raw px/s: a raw pixel threshold silently under-detects
# swings on a camera framed wider than whatever distance this was tuned
# against (same generalization risk shots.mph_from_norm already avoids for
# ball speed by normalizing against the session's own court pixel width)
REFERENCE_COURT_WIDTH_PX = 900.0  # approx. court width this threshold assumes
SWING_MIN_NORM_S = 250.0 / REFERENCE_COURT_WIDTH_PX


BAND_LO_HZ = 1200      # paddle pop concentrates 1-6 kHz; speech fundamentals,
BAND_HI_HZ = 6000      # court rumble and music bass live outside this band
ATTACK_RATIO = 2.5     # post/pre RMS ratio a true transient must exceed


def detect_hits(audio_path, sr_target: int = 22050) -> np.ndarray:
    """Hit times (seconds) from session audio, robust to speech/music/wind.

    Band-pass isolates the paddle-pop band, then a transient gate keeps only
    onsets with a sharp attack (sustained sounds ramp; pops don't).
    """
    import librosa  # local import: heavy, and audio may be absent entirely
    from scipy.signal import butter, sosfiltfilt

    y, sr = librosa.load(str(audio_path), sr=sr_target, mono=True)
    if len(y) < sr:  # under a second of audio
        return np.array([])
    sos = butter(4, [BAND_LO_HZ, BAND_HI_HZ], btype="band", fs=sr, output="sos")
    yf = sosfiltfilt(sos, y)
    onset_env = librosa.onset.onset_strength(y=yf, sr=sr)
    onsets = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, units="time",
        backtrack=False, delta=0.35, wait=int(MIN_HIT_GAP_S * sr / 512),
    )
    keep = []
    win = int(0.010 * sr)
    for t in np.asarray(onsets):
        i = int(t * sr)
        # onset timestamps lag the physical pop by up to a hop (~23 ms), so
        # look for the energy peak in a window straddling t
        lo, hi = max(0, i - int(0.06 * sr)), min(len(yf), i + int(0.03 * sr))
        peaks = [np.sqrt(np.mean(yf[j:j + win] ** 2) + 1e-12)
                 for j in range(lo, hi - win, win // 2)]
        peak = max(peaks) if peaks else 0.0
        base_seg = yf[max(0, i - int(0.15 * sr)):max(1, i - int(0.05 * sr))]
        base = np.sqrt(np.mean(base_seg ** 2) + 1e-12)
        if peak / base >= ATTACK_RATIO:
            keep.append(float(t))
    return np.asarray(keep)


def segment_rallies(hit_times: np.ndarray) -> list[dict]:
    """Group hit times into rallies split on RALLY_GAP_S silences."""
    if len(hit_times) == 0:
        return []
    rallies, group = [], [float(hit_times[0])]
    for t in hit_times[1:]:
        if t - group[-1] > RALLY_GAP_S:
            rallies.append(group)
            group = []
        group.append(float(t))
    rallies.append(group)
    return [
        {"start": g[0], "end": g[-1], "hits": len(g), "duration": round(g[-1] - g[0], 2)}
        for g in rallies if len(g) >= MIN_RALLY_HITS
    ]


def wrist_speed(sub: pd.DataFrame, fps: float, court_width_px: float) -> pd.DataFrame:
    """Per-frame max wrist speed (court-widths/s) for the subject track.

    Keypoints at (0,0) mean the wrist wasn't detected in that frame — masked out.
    Normalized by the session's own court pixel width (same reasoning as
    shots.mph_from_norm) so SWING_MIN_NORM_S means the same real swing speed
    regardless of how tightly the camera frames the court.
    """
    sub = sub.sort_values("frame").reset_index(drop=True)
    t = sub["frame"].to_numpy() / fps
    speeds = []
    for pre in ("lw", "rw"):
        xy = sub[[f"{pre}x", f"{pre}y"]].to_numpy()
        ok = (xy != 0).all(axis=1)
        v = np.full(len(sub), np.nan)
        d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])) / np.clip(np.diff(t), 1e-3, None)
        valid = ok[1:] & ok[:-1]
        v[1:][valid] = d[valid]
        speeds.append(v)
    stacked = np.column_stack(speeds)
    best = np.full(len(stacked), np.nan)
    any_valid = ~np.isnan(stacked).all(axis=1)  # frames where neither wrist seen stay NaN
    best[any_valid] = np.nanmax(stacked[any_valid], axis=1)
    return pd.DataFrame({"t": t, "speed": best / court_width_px})


def detect_swings(ws: pd.DataFrame) -> np.ndarray:
    """Swing times: local maxima of wrist speed above threshold, debounced."""
    s = ws["speed"].to_numpy()
    t = ws["t"].to_numpy()
    swings = []
    for i in range(1, len(s) - 1):
        if (np.isfinite(s[i]) and s[i] >= SWING_MIN_NORM_S
                and s[i] >= np.nan_to_num(s[i - 1]) and s[i] >= np.nan_to_num(s[i + 1])
                and (not swings or t[i] - swings[-1] > MIN_HIT_GAP_S)):
            swings.append(float(t[i]))
    return np.asarray(swings)


def attribute_hits(hit_times: np.ndarray, swing_times: np.ndarray) -> np.ndarray:
    """Boolean per hit: True when a subject swing coincides with the hit sound."""
    if len(swing_times) == 0:
        return np.zeros(len(hit_times), dtype=bool)
    return np.array([bool(np.min(np.abs(swing_times - h)) <= SWING_MATCH_S)
                     for h in hit_times])


def corroborate_hits(hit_times: np.ndarray,
                     player_swings: dict[str, np.ndarray]) -> np.ndarray:
    """Drop audio "hits" with no matching wrist swing from any tracked player.

    Pre-play noise (footsteps, paddle taps while setting up, talking) can
    pass the audio transient gate but has no swing behind it — filtering on
    swing corroboration keeps warmup/dead air from being scored as a rally.
    Generalizes from 2 players (singles) to up to 4 (doubles) — a hit counts
    if ANY tracked player has a matching swing. If nobody has any tracked
    swings at all (e.g. tracking degraded), skip filtering rather than
    silently zeroing out every rally.
    """
    if len(hit_times) == 0:
        return hit_times
    if all(len(s) == 0 for s in player_swings.values()):
        return hit_times
    ok = np.zeros(len(hit_times), dtype=bool)
    for swings in player_swings.values():
        ok |= attribute_hits(hit_times, swings)
    return hit_times[ok]


def rally_metrics(rallies: list[dict], hit_times: np.ndarray,
                  subject_hits: np.ndarray, video_duration: float) -> dict:
    if not rallies:
        return {"rally_count": 0, "note": "no rallies detected (missing/quiet audio?)"}
    hits_per = [r["hits"] for r in rallies]
    durations = [r["duration"] for r in rallies]
    play_time = float(sum(durations))
    return {
        "rally_count": len(rallies),
        "total_hits": int(len(hit_times)),
        "subject_shots": int(subject_hits.sum()),
        "avg_rally_hits": round(float(np.mean(hits_per)), 1),
        "max_rally_hits": int(max(hits_per)),
        "avg_rally_seconds": round(float(np.mean(durations)), 1),
        "play_time_pct": round(100.0 * play_time / video_duration, 1) if video_duration else 0.0,
        "rallies": rallies,
    }
