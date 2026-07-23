"""Player detection + tracking (YOLOv8-pose + ByteTrack) and subject selection.

Output: per-frame track rows (frame, track_id, x1, y1, x2, y2, conf, wrist
keypoints) plus the subject's projected court positions. Feet position =
bottom-center of bbox. Wrists feed M2 swing detection.
"""

from pathlib import Path

import numpy as np
import pandas as pd
from ultralytics import YOLO

from court import NET_Y, CourtCalibration, on_court

MODEL_NAME = "yolov8s-pose.pt"
# COCO keypoint indices
KP_LWRIST, KP_RWRIST = 9, 10

COLUMNS = ["frame", "track_id", "x1", "y1", "x2", "y2", "conf",
           "lwx", "lwy", "rwx", "rwy"]


TRACK_STRIDE = 2  # process every 2nd frame (15 fps effective); 1 = accuracy mode


def run_tracking(video: Path, out_parquet: Path, models_dir: Path,
                 progress_cb=None, frame_cb=None,
                 stride: int | None = None) -> pd.DataFrame:
    """Track all persons through the video; write tracks parquet.

    Single-decode integration: frame_cb(real_frame_idx, orig_img, boxes_xyxy)
    is invoked per processed frame so cheap consumers (ball detection) reuse
    the same decode instead of reading the video again. Frame numbers written
    to the parquet are REAL video frame indices (i * stride).
    """
    if stride is None:
        stride = TRACK_STRIDE  # resolved at call time so tests/config can override
    model = YOLO(str(models_dir / MODEL_NAME))
    rows = []
    # NOTE: do NOT pass half=True — on MPS it hits a deprecated fp16 fallback
    # that is ~27x SLOWER (measured 1430 ms/frame vs 53 ms/frame)
    results = model.track(
        source=str(video), stream=True, persist=True,
        classes=[0], tracker="bytetrack.yaml", verbose=False,
        vid_stride=stride,
    )
    for i, r in enumerate(results):
        real_f = i * stride
        xyxy = None
        if r.boxes is not None and r.boxes.id is not None:
            ids = r.boxes.id.cpu().numpy().astype(int)
            xyxy = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()
            if r.keypoints is not None and r.keypoints.xy is not None:
                kps = r.keypoints.xy.cpu().numpy()  # (n, 17, 2); (0,0) = undetected
            else:
                kps = np.zeros((len(ids), 17, 2))
            for tid, box, conf, kp in zip(ids, xyxy, confs, kps):
                rows.append((real_f, tid, *box.tolist(), float(conf),
                             *kp[KP_LWRIST].tolist(), *kp[KP_RWRIST].tolist()))
        if frame_cb is not None:
            frame_cb(real_f, r.orig_img, xyxy)
        if progress_cb and i % 30 == 0:
            progress_cb(real_f)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_parquet(out_parquet, index=False)
    return df


def feet_px(df: pd.DataFrame) -> np.ndarray:
    """Bottom-center of each bbox: the feet contact point used for homography."""
    return np.stack([(df["x1"] + df["x2"]) / 2.0, df["y2"]], axis=1)


def pick_subject(df: pd.DataFrame, click_xy: tuple[float, float],
                 first_n_frames: int = 90) -> int:
    """Track id whose early bbox center is nearest the user's 'this is me' click."""
    early = df[df["frame"] <= df["frame"].min() + first_n_frames]
    if early.empty:
        raise ValueError("no tracks found in video")
    cx = (early["x1"] + early["x2"]) / 2.0
    cy = (early["y1"] + early["y2"]) / 2.0
    d2 = (cx - click_xy[0]) ** 2 + (cy - click_xy[1]) ** 2
    best = early.assign(d2=d2).groupby("track_id")["d2"].min()
    return int(best.idxmin())


MAX_STITCH_GAP = 45      # frames the subject may vanish before we stop following
STITCH_OVERLAP = 20      # a continuation may START this many frames BEFORE the
                         # current fragment ends (tracker fragments overlap)
STITCH_JUMP_PX = 12.0    # allowed bbox-center drift per missing frame
STITCH_BASE_PX = 60.0    # base allowance: an id switch mid-motion jumps a bit


def stitch_subject(df: pd.DataFrame, first_id: int) -> pd.DataFrame:
    """Follow the subject across track-id breaks by position continuity.

    Trackers fragment ids on occlusion or camera motion (broadcast footage
    produced 485 ids for 4 players), and the replacement id often starts a few
    frames BEFORE the old one dies. Starting from the clicked track, whenever
    the current fragment ends we adopt the unused track whose position at the
    handover point is nearest — searching a window that includes that overlap.
    """
    df = df.sort_values("frame")
    cx = (df["x1"] + df["x2"]) / 2.0
    cy = (df["y1"] + df["y2"]) / 2.0
    df = df.assign(cx=cx, cy=cy)
    by_id = {tid: g.reset_index(drop=True) for tid, g in df.groupby("track_id")}
    starts = {tid: int(g["frame"].iloc[0]) for tid, g in by_id.items()}

    chain = [first_id]
    used = {first_id}
    cur = by_id[first_id]
    while True:
        last = cur.iloc[-1]
        last_f = int(last["frame"])
        best_id, best_score = None, np.inf
        for tid, f0 in starts.items():
            if tid in used or not (last_f - STITCH_OVERLAP < f0 <= last_f + MAX_STITCH_GAP):
                continue
            g = by_id[tid]
            # candidate's position at the handover point (>= last_f if overlapping)
            at = g[g["frame"] >= last_f]
            head = at.iloc[0] if len(at) else g.iloc[-1]
            gap = max(0, f0 - last_f)
            d = np.hypot(head["cx"] - last["cx"], head["cy"] - last["cy"])
            if d <= STITCH_BASE_PX + STITCH_JUMP_PX * gap and d + 2.0 * gap < best_score:
                best_id, best_score = tid, d + 2.0 * gap
        if best_id is None:
            break
        used.add(best_id)
        chain.append(best_id)
        cur = by_id[best_id]

    out = df[df["track_id"].isin(chain)].drop(columns=["cx", "cy"])
    # a frame can appear in overlapping fragments — keep the first occurrence
    return out.sort_values("frame").drop_duplicates("frame").reset_index(drop=True)


def stitch_chain_ids(df: pd.DataFrame, first_id: int) -> set[int]:
    """Track ids in the subject's stitched chain (see stitch_subject)."""
    return set(stitch_subject(df, first_id)["track_id"].unique().tolist())


MIN_OPPONENT_FRAMES = 30


def pick_opponents(df: pd.DataFrame, calib: CourtCalibration,
                   exclude_ids: set[int], subject_median_y: float,
                   n: int = 1) -> list[int]:
    """Track ids most likely to be opponent(s), best-scoring first.

    Heuristic: exclude the subject's (and, in doubles, partner's) own
    chain(s), then score every remaining track by (on-court frame count) x
    (fraction of those frames on the opposite side of the net from the
    subject). This favors consistently visible players facing the subject
    over a stray spectator/ball-kid detection or a doubles partner standing
    on the subject's own side. Returns up to `n` ids, ranked by score —
    fewer if not enough candidates qualify (e.g. subject not tracked long
    enough to establish a side, or nobody else visible).
    """
    candidates = df[~df["track_id"].isin(exclude_ids)]
    if candidates.empty:
        return []
    subject_side = np.sign(subject_median_y - NET_Y)
    scored: list[tuple[float, int]] = []
    for tid, g in candidates.groupby("track_id"):
        if len(g) < MIN_OPPONENT_FRAMES:
            continue
        pts = calib.to_court(feet_px(g))
        on = np.array([on_court(x, y) for x, y in pts])
        if not on.any():
            continue
        ys = pts[on, 1]
        opp_frac = float(np.mean(np.sign(ys - NET_Y) != subject_side))
        score = on.sum() * opp_frac
        if score > 0:
            scored.append((score, int(tid)))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [tid for _, tid in scored[:n]]


def pick_opponent(df: pd.DataFrame, calib: CourtCalibration,
                  subject_ids: set[int], subject_median_y: float) -> int | None:
    """Singles convenience wrapper around pick_opponents (n=1)."""
    result = pick_opponents(df, calib, subject_ids, subject_median_y, n=1)
    return result[0] if result else None


def _cluster_person_chains(df: pd.DataFrame, ids: set[int]) -> list[set[int]]:
    """Group leftover track ids into person-chains via the same
    continuity logic as stitch_subject, so one real person fragmented
    across many tracker re-ids (occlusion, camera motion) is counted once
    instead of once per fragment. Seeds chains in first-appearance order
    so an earlier fragment always claims its own later fragments first.
    """
    remaining = df[df["track_id"].isin(ids)]
    if remaining.empty:
        return []
    order = remaining.groupby("track_id")["frame"].min().sort_values().index.tolist()
    seen: set[int] = set()
    chains: list[set[int]] = []
    for tid in order:
        if tid in seen:
            continue
        chain = stitch_chain_ids(remaining, tid)
        seen |= chain
        chains.append(chain)
    return chains


def count_secondary_court_tracks(df: pd.DataFrame, calib: CourtCalibration,
                                 exclude_ids: set[int]) -> int:
    """Count other on-court PEOPLE besides all already-identified players
    that meet the same visibility bar as an opponent candidate
    (MIN_OPPONENT_FRAMES total frames, ever on-court). A nonzero count
    suggests an unexpected extra person (crowded court) — callers should
    only treat this as a doubles-ambiguity signal in singles mode, since
    doubles already expects 4 identified players on court.

    Clusters raw track ids into person-chains first (see
    _cluster_person_chains) — without this, a single real person whose
    tracker id fragments repeatedly (very possible on non-tripod footage;
    stitch_subject's own docs note 485 ids for 4 players on one broadcast
    clip) gets counted as a new "extra person" every time they're re-id'd.
    """
    candidate_ids = set(df["track_id"].unique()) - exclude_ids
    if not candidate_ids:
        return 0
    count = 0
    for chain in _cluster_person_chains(df, candidate_ids):
        g = df[df["track_id"].isin(chain)]
        if len(g) < MIN_OPPONENT_FRAMES:
            continue
        pts = calib.to_court(feet_px(g))
        on = np.array([on_court(x, y) for x, y in pts])
        if on.any():
            count += 1
    return count


def subject_court_positions(df: pd.DataFrame, subject_id: int,
                            calib: CourtCalibration, fps: float) -> pd.DataFrame:
    """Subject's feet in court feet-coords per frame, off-court points dropped.

    Follows the subject across tracker id fragmentation via stitch_subject.
    """
    sub = stitch_subject(df, subject_id)
    pts = calib.to_court(feet_px(sub))
    out = pd.DataFrame({
        "frame": sub["frame"].to_numpy(),
        "t": sub["frame"].to_numpy() / fps,
        "x": pts[:, 0],
        "y": pts[:, 1],
    })
    mask = [on_court(x, y) for x, y in zip(out["x"], out["y"])]
    return out[mask].reset_index(drop=True)
