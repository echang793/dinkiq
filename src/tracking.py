"""Player detection + tracking (YOLOv8-pose + ByteTrack) and subject selection.

Output: per-frame track rows (frame, track_id, x1, y1, x2, y2, conf, wrist
keypoints) plus the subject's projected court positions. Feet position =
bottom-center of bbox. Wrists feed M2 swing detection.
"""

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
from ultralytics import YOLO

from court import NET_Y, CourtCalibration, on_court_mask
from jsonio import atomic_write_json

log = logging.getLogger("dinkiq.tracking")

MODEL_NAME = "yolov8s-pose.pt"
POSE_IMGSZ_DEFAULT = 960   # far-court players are small; 960 sees them better than 640
# COCO keypoint indices
KP_LWRIST, KP_RWRIST = 9, 10

COLUMNS = ["frame", "track_id", "x1", "y1", "x2", "y2", "conf",
           "lwx", "lwy", "rwx", "rwy"]


TRACK_STRIDE = 2  # process every 2nd frame (15 fps effective); 1 = accuracy mode


def pose_imgsz() -> int:
    """Pose-model input size (DINKIQ_POSE_IMGSZ overrides; garbage -> default)."""
    try:
        return int(os.environ.get("DINKIQ_POSE_IMGSZ", POSE_IMGSZ_DEFAULT))
    except ValueError:
        return POSE_IMGSZ_DEFAULT


def select_device() -> str:
    """Where to run the pose model. ultralytics only picks the GPU if asked, so the
    default was the CPU: measured 32 ms/frame at 640 vs 10.6 ms on Apple-silicon MPS
    (16.8 ms at 960, still 2x faster than the old default). DINKIQ_DEVICE overrides."""
    forced = os.environ.get("DINKIQ_DEVICE", "").strip()
    if forced:
        return forced
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def tracks_are_stale(meta: dict | None) -> bool:
    """True when cached tracks came from a different pose model or input size than
    the current settings, so a re-analysis should redo detection instead of
    silently reusing them. Tracks without metadata predate this check and are
    trusted: never throw away minutes of work on a guess. (device doesn't matter;
    results are the same either way.)"""
    if not meta:
        return False
    return meta.get("model") != MODEL_NAME or meta.get("imgsz") != pose_imgsz()


def run_tracking(video: Path, out_parquet: Path, models_dir: Path,
                 progress_cb=None, frame_cb=None,
                 stride: int | None = None) -> pd.DataFrame:
    """Track all persons through the video; write tracks parquet.

    Single-decode integration: frame_cb(real_frame_idx, orig_img, boxes_xyxy)
    is invoked per processed frame so cheap consumers (ball detection) reuse
    the same decode instead of reading the video again. Frame numbers written
    to the parquet are REAL video frame indices (i * stride).

    Runs on the GPU when there is one; if that fails before the first frame (an
    unsupported op on MPS, say) it retries once on the CPU rather than failing
    the analysis. The model/size/device used are written next to the parquet
    (tracks.meta.json) so a later change of model can be told apart from a cache hit.
    """
    if stride is None:
        stride = TRACK_STRIDE  # resolved at call time so tests/config can override
    device, imgsz = select_device(), pose_imgsz()
    attempts = [device] if device == "cpu" else [device, "cpu"]
    for k, dev in enumerate(attempts):
        model = YOLO(str(models_dir / MODEL_NAME))  # fresh model: its tracker state is per-instance
        rows: list[tuple] = []
        n = 0
        try:
            # NOTE: do NOT pass half=True — on MPS it hits a deprecated fp16 fallback
            # that is ~27x SLOWER (measured 1430 ms/frame vs 53 ms/frame)
            results = model.track(
                source=str(video), stream=True, persist=True,
                classes=[0], tracker="bytetrack.yaml", verbose=False,
                vid_stride=stride, imgsz=imgsz, device=dev,
            )
            for i, r in enumerate(results):
                n += 1
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
            device = dev
            break
        except Exception:
            if n > 0 or k == len(attempts) - 1:
                raise  # mid-video, or nothing left to fall back to
            log.warning("pose model failed on %s before the first frame; retrying on cpu",
                        dev, exc_info=True)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_parquet(out_parquet, index=False)
    atomic_write_json(out_parquet.with_name("tracks.meta.json"),
                      {"model": MODEL_NAME, "imgsz": imgsz, "device": device, "stride": stride})
    return df


def feet_px(df: pd.DataFrame) -> np.ndarray:
    """Bottom-center of each bbox: the feet contact point used for homography."""
    return np.stack([(df["x1"] + df["x2"]) / 2.0, df["y2"]], axis=1)


class PlayerNotFound(ValueError):
    """The tap didn't land on anyone. A ValueError so existing handlers still catch it."""


PICK_MAX_DIST = 0.5     # body-heights outside a bbox that still counts as "tapped on them"
PICK_WINDOW = 90        # frames (3 s) searched first, then PICK_WIDE_WINDOW
PICK_WIDE_WINDOW = 300  # 10 s: a player not detected in the very first frames


def _box_distances(df: pd.DataFrame, click_xy: tuple[float, float]) -> tuple[pd.Series, pd.Series]:
    """Per row: distance from the click to the bbox (0 when inside) and to its
    centre, both in body-heights so far-court (small) and near-court players are
    judged by the same rule."""
    px, py = click_xy
    h = (df["y2"] - df["y1"]).clip(lower=1.0)
    dx = np.maximum.reduce([df["x1"] - px, np.zeros(len(df)), px - df["x2"]])
    dy = np.maximum.reduce([df["y1"] - py, np.zeros(len(df)), py - df["y2"]])
    cx, cy = (df["x1"] + df["x2"]) / 2.0, (df["y1"] + df["y2"]) / 2.0
    return (pd.Series(np.hypot(dx, dy), index=df.index) / h,
            pd.Series(np.hypot(cx - px, cy - py), index=df.index) / h)


def pick_subject(df: pd.DataFrame, click_xy: tuple[float, float],
                 first_n_frames: int = PICK_WINDOW,
                 exclude_ids: set[int] | frozenset[int] | None = None,
                 max_dist: float = PICK_MAX_DIST) -> int:
    """Track id of the person the user tapped on in the first frame.

    The old version returned the nearest track however far away it was: a tap on
    an undetected partner silently resolved to the SUBJECT's own track (the sample
    had it 2.3 body-heights off), and "nearest over 90 frames" let a passer-by
    crossing the spot win. Now the tap must land on (or within max_dist body-
    heights of) a detection; the earliest frame with one wins, ties broken by the
    nearest centre; exclude_ids keeps a partner from resolving to the subject. The
    search widens once (to 10 s) for someone not detected yet, then raises
    PlayerNotFound rather than guessing."""
    if df.empty:
        raise ValueError("no tracks found in video")
    exclude = set(exclude_ids or ())
    f0 = int(df["frame"].min())
    for window in dict.fromkeys((first_n_frames, max(first_n_frames, PICK_WIDE_WINDOW))):
        early = df[(df["frame"] <= f0 + window) & ~df["track_id"].isin(exclude)]
        if early.empty:
            continue
        d_box, d_ctr = _box_distances(early, click_xy)
        ok = early[d_box <= max_dist]
        if ok.empty:
            continue
        first = ok[ok["frame"] == ok["frame"].min()]
        score = d_box[first.index] * 10.0 + d_ctr[first.index]
        return int(first.loc[score.idxmin(), "track_id"])
    raise PlayerNotFound("Couldn't find a player where you tapped on the first frame — "
                         "recalibrate and tap directly on the player.")


MAX_STITCH_GAP = 45      # frames the subject may vanish before we stop following
STITCH_OVERLAP = 20      # a continuation may START this many frames BEFORE the
                         # current fragment ends (tracker fragments overlap)
STITCH_JUMP_PX = 12.0    # allowed bbox-center drift per missing frame
STITCH_BASE_PX = 60.0    # base allowance: an id switch mid-motion jumps a bit


class TrackIndex:
    """A tracks frame grouped by id once, so stitching can be asked many times
    (subject, partner, each opponent, every leftover person) without re-sorting
    and re-grouping the whole table each time -- it used to be done 6-11 times per
    analysis plus once per candidate in count_secondary_court_tracks."""

    def __init__(self, df: pd.DataFrame):
        d = df.sort_values("frame", kind="stable")
        self.df = d.assign(cx=(d["x1"] + d["x2"]) / 2.0, cy=(d["y1"] + d["y2"]) / 2.0)
        self.by_id = {tid: g.reset_index(drop=True) for tid, g in self.df.groupby("track_id")}
        self.starts = {tid: int(g["frame"].iloc[0]) for tid, g in self.by_id.items()}

    def chain_ids(self, first_id: int, exclude=frozenset()) -> list[int]:
        """Follow one person across track-id breaks by position continuity.

        Trackers fragment ids on occlusion or camera motion (broadcast footage
        produced 485 ids for 4 players), and the replacement id often starts a few
        frames BEFORE the old one dies. Starting from first_id, whenever the
        current fragment ends we adopt the unused track whose position at the
        handover point is nearest -- searching a window that includes that
        overlap. `exclude` holds ids that belong to someone else (already-chosen
        players), so two people can never claim the same fragment."""
        chain, used = [first_id], {first_id}
        cur = self.by_id[first_id]
        while True:
            last = cur.iloc[-1]
            last_f = int(last["frame"])
            best_id, best_score = None, np.inf
            for tid, f0 in self.starts.items():
                if tid in used or tid in exclude or not (last_f - STITCH_OVERLAP < f0 <= last_f + MAX_STITCH_GAP):
                    continue
                g = self.by_id[tid]
                # candidate's position at the handover point (>= last_f if overlapping)
                at = g[g["frame"] >= last_f]
                head = at.iloc[0] if len(at) else g.iloc[-1]
                gap = max(0, f0 - last_f)
                d = np.hypot(head["cx"] - last["cx"], head["cy"] - last["cy"])
                if d <= STITCH_BASE_PX + STITCH_JUMP_PX * gap and d + 2.0 * gap < best_score:
                    best_id, best_score = tid, d + 2.0 * gap
            if best_id is None:
                return chain
            used.add(best_id)
            chain.append(best_id)
            cur = self.by_id[best_id]

    def frame_for(self, ids) -> pd.DataFrame:
        out = self.df[self.df["track_id"].isin(list(ids))].drop(columns=["cx", "cy"])
        # a frame can appear in overlapping fragments — keep the first occurrence
        return out.sort_values("frame", kind="stable").drop_duplicates("frame").reset_index(drop=True)

    def cluster(self, ids) -> list[list[int]]:
        """Group ids into person-chains, seeded in first-appearance order so an
        earlier fragment claims its own later fragments first. Chains only ever
        contain ids from `ids`."""
        allowed = set(ids)
        outside = set(self.by_id) - allowed
        seen: set[int] = set()
        chains = []
        for tid in sorted(allowed, key=lambda t: self.starts[t]):
            if tid in seen:
                continue
            chain = self.chain_ids(tid, exclude=outside | seen)
            seen |= set(chain)
            chains.append(chain)
        return chains


def stitch_subject(df: pd.DataFrame, first_id: int, exclude_ids=None,
                   index: TrackIndex | None = None) -> pd.DataFrame:
    """The person's rows across id breaks (see TrackIndex.chain_ids). Pass an
    index to avoid rebuilding it, exclude_ids to keep players distinct."""
    idx = index or TrackIndex(df)
    return idx.frame_for(idx.chain_ids(first_id, frozenset(exclude_ids or ())))


def stitch_chain_ids(df: pd.DataFrame, first_id: int, exclude_ids=None,
                     index: TrackIndex | None = None) -> set[int]:
    """Track ids in the person's stitched chain."""
    idx = index or TrackIndex(df)
    return set(idx.chain_ids(first_id, frozenset(exclude_ids or ())))


MIN_OPPONENT_FRAMES = 30
BEHIND_BASELINE_FT = 8.0


def _side_lookup(subject_pos: pd.DataFrame | None, fallback_y: float):
    """frame -> which side of the net the subject is on at that moment (+1/-1),
    by the nearest sample within 2 s; the session-median side where there is none.
    Players switch ends between games, so one side for the whole session is wrong
    for half of a long recording."""
    fallback = float(np.sign(fallback_y - NET_Y)) or 1.0
    if subject_pos is None or not len(subject_pos):
        return lambda frames: np.full(len(frames), fallback)
    pf = subject_pos["frame"].to_numpy()
    order = np.argsort(pf)
    pf = pf[order]
    sides = np.sign(subject_pos["y"].to_numpy()[order] - NET_Y)

    def look(frames: np.ndarray) -> np.ndarray:
        i = np.clip(np.searchsorted(pf, frames), 1, len(pf) - 1) if len(pf) > 1 else np.zeros(len(frames), int)
        left, right = np.clip(i - 1, 0, len(pf) - 1), np.clip(i, 0, len(pf) - 1)
        pick = np.where(np.abs(pf[left] - frames) <= np.abs(pf[right] - frames), left, right)
        near = np.abs(pf[pick] - frames) <= 60
        out = np.where(near, sides[pick], fallback)
        return np.where(out == 0, fallback, out)
    return look


def pick_opponent_chains(df: pd.DataFrame, calib: CourtCalibration,
                         exclude_ids, subject_pos: pd.DataFrame | None,
                         subject_median_y: float, n: int = 1,
                         index: TrackIndex | None = None) -> list[list[int]]:
    """The n most likely opponents as person-chains (seed id first), best first.

    Every id not in exclude_ids is grouped into person-chains FIRST, then each
    chain is scored by (on-court frames) x (fraction of those frames on the
    opposite side of the net from the subject AT THAT MOMENT). Scoring raw ids
    meant doubles' "two opponents" could be two fragments of one person; a single
    session-median side broke when players switched ends. Spectators and stray
    detections lose on frame count; a doubles partner loses on side."""
    idx = index or TrackIndex(df)
    cands = [t for t in idx.by_id if t not in set(exclude_ids)]
    if not cands:
        return []
    side_of = _side_lookup(subject_pos, subject_median_y)
    scored: list[tuple[float, list[int]]] = []
    for chain in idx.cluster(cands):
        g = idx.frame_for(chain)
        if len(g) < MIN_OPPONENT_FRAMES:
            continue
        pts = calib.to_court(feet_px(g))
        on = on_court_mask(pts)
        if not on.any():
            continue
        subj_side = side_of(g["frame"].to_numpy()[on])
        opp_frac = float(np.mean(np.sign(pts[on, 1] - NET_Y) != subj_side))
        score = on.sum() * opp_frac
        if score > 0:
            scored.append((score, chain))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [c for _, c in scored[:n]]


def pick_opponents(df: pd.DataFrame, calib: CourtCalibration,
                   exclude_ids: set[int], subject_median_y: float,
                   n: int = 1, subject_pos: pd.DataFrame | None = None,
                   index: TrackIndex | None = None) -> list[int]:
    """Seed track id of each likely opponent, best first (see pick_opponent_chains).
    Fewer than n if not enough candidates qualify."""
    return [c[0] for c in pick_opponent_chains(
        df, calib, exclude_ids, subject_pos, subject_median_y, n=n, index=index)]


def pick_opponent(df: pd.DataFrame, calib: CourtCalibration,
                  subject_ids: set[int], subject_median_y: float) -> int | None:
    """Singles convenience wrapper around pick_opponents (n=1)."""
    result = pick_opponents(df, calib, subject_ids, subject_median_y, n=1)
    return result[0] if result else None


def count_secondary_court_tracks(df: pd.DataFrame, calib: CourtCalibration,
                                 exclude_ids: set[int],
                                 index: TrackIndex | None = None) -> int:
    """Count other on-court PEOPLE besides all already-identified players
    that meet the same visibility bar as an opponent candidate
    (MIN_OPPONENT_FRAMES total frames, ever on-court). A nonzero count
    suggests an unexpected extra person (crowded court) — callers should
    only treat this as a doubles-ambiguity signal in singles mode, since
    doubles already expects 4 identified players on court.

    Clusters raw track ids into person-chains first — without this, a single
    real person whose tracker id fragments repeatedly (very possible on
    non-tripod footage; one broadcast clip had 485 ids for 4 players) gets
    counted as a new "extra person" every time they're re-id'd. Uses a shared
    TrackIndex: this used to re-sort and re-group the entire table once per
    candidate id (quadratic in the number of ids).
    """
    idx = index or TrackIndex(df)
    candidate_ids = set(idx.by_id) - set(exclude_ids)
    if not candidate_ids:
        return 0
    count = 0
    for chain in idx.cluster(candidate_ids):
        g = idx.frame_for(chain)
        if len(g) < MIN_OPPONENT_FRAMES:
            continue
        if on_court_mask(calib.to_court(feet_px(g))).any():
            count += 1
    return count


def subject_court_positions(df: pd.DataFrame, subject_id: int,
                            calib: CourtCalibration, fps: float,
                            chain: pd.DataFrame | None = None) -> pd.DataFrame:
    """Subject's feet in court feet-coords per frame, off-court points dropped.

    Follows the subject across tracker id fragmentation via stitch_subject, or
    uses `chain` (an already-stitched frame) so callers that have it don't pay
    to stitch again.
    """
    sub = chain if chain is not None else stitch_subject(df, subject_id)
    pts = calib.to_court(feet_px(sub))
    out = pd.DataFrame({
        "frame": sub["frame"].to_numpy(),
        "t": sub["frame"].to_numpy() / fps,
        "x": pts[:, 0],
        "y": pts[:, 1],
    })
    # 4 ft past a sideline, 8 ft past a baseline: returners stand 2-6 ft behind it, and
    # dropping them as "off court" undercounted baseline time (inflating kitchen time)
    mask = on_court_mask(out[["x", "y"]].to_numpy(), margin=4.0, margin_y=BEHIND_BASELINE_FT)
    return out[mask].reset_index(drop=True)
