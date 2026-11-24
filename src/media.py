"""ffmpeg/ffprobe: probing, ingest (normalize + audio + first frame), highlight reel."""

import json
import logging
import subprocess
from pathlib import Path

import notify
from jsonio import atomic_write_json, load_json
from pipeline import FPS
from session_status import set_status

log = logging.getLogger("dinkiq.media")


class ToolMissing(RuntimeError):
    """ffmpeg/ffprobe isn't on PATH. launchd hands services a minimal PATH with
    no Homebrew, which once left the server serving happily while every analysis
    died with a raw "[Errno 2] ... 'ffprobe'"; say what is actually wrong."""


def _tool_missing(name: str) -> ToolMissing:
    return ToolMissing(f"{name} not found — is ffmpeg installed and on the service's PATH?")


def _run(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise _tool_missing(cmd[0]) from e
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr[-800:]}")


def _has_audio(raw: Path) -> bool:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(raw)],
            capture_output=True, text=True)
    except FileNotFoundError as e:
        raise _tool_missing("ffprobe") from e
    return bool(proc.stdout.strip())


def _parse_rate(rate: str | None) -> float:
    num, _, den = (rate or "0/1").partition("/")
    try:
        d = float(den or 1)
        return float(num) / d if d else 0.0
    except ValueError:
        return 0.0


def _rotation_of(stream: dict) -> int:
    """0/90/180/270. Phones record landscape pixels plus a rotation flag, stored
    either as display-matrix side data (newer ffmpeg) or a `rotate` tag (older)."""
    raw = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            raw = sd["rotation"]
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    try:
        return abs(int(float(raw))) % 360
    except (TypeError, ValueError):
        return 0


def probe_video(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames"
             ":stream_tags=rotate:stream_side_data=rotation:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True)
    except FileNotFoundError as e:
        raise _tool_missing("ffprobe") from e
    info = json.loads(proc.stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    fps = _parse_rate(st.get("r_frame_rate"))
    avg = _parse_rate(st.get("avg_frame_rate"))
    duration = float(info.get("format", {}).get("duration") or 0.0)
    frames = int(st.get("nb_frames") or 0) or int(duration * fps)
    width, height = int(st.get("width") or 0), int(st.get("height") or 0)
    rotation = _rotation_of(st)
    dw, dh = (height, width) if rotation in (90, 270) else (width, height)
    return {"codec": st.get("codec_name"), "width": width, "height": height, "fps": round(fps, 3),
            "frames": frames, "duration": round(duration, 2), "rotation": rotation,
            # r_frame_rate is the container's nominal rate, avg_frame_rate what the
            # frames actually average to: they diverge on variable-frame-rate video
            "vfr": bool(fps and avg and abs(avg - fps) / fps > 0.01),
            "portrait": dh > dw}


TARGET_HEIGHT = 720
FPS_TOLERANCE = 0.01   # 29.97 differs from 30 by 0.03: ~4 s of drift against the audio per hour


def normalize_plan(src: dict) -> dict:
    """Remux (fast, lossless) only when the upload already IS the analysis format:
    H.264, exactly 720p, constant 30 fps, upright. Everything else is re-encoded.

    The old rule remuxed any H.264 <= 720p at 29-31 fps, which kept a 360p clip at
    360p (every pixel threshold downstream is tuned for 720p), let 29.97 fps drift
    ~4 s against the audio over an hour (frame -> time is a fixed /30), and kept a
    phone's rotation as side-data that OpenCV may ignore."""
    reasons = []
    if src.get("codec") != "h264":
        reasons.append(f"codec {src.get('codec')} is re-encoded to h264")
    h = src.get("height", 0)
    if h < TARGET_HEIGHT:
        reasons.append(f"upscale to 720p (source is {h}p; the analysis thresholds assume 720p)")
    elif h > TARGET_HEIGHT:
        reasons.append(f"downscale to 720p (source is {h}p)")
    if abs(src.get("fps", 0.0) - FPS) > FPS_TOLERANCE:
        reasons.append(f"fps {src.get('fps')} is re-timed to a constant 30")
    if src.get("vfr"):
        reasons.append("variable frame rate needs a constant 30 fps")
    if src.get("rotation", 0):
        reasons.append(f"rotation {src['rotation']} must be applied to the pixels")
    return {"mode": "reencode" if reasons else "copy", "reason": "; ".join(reasons)}


def normalize_command(raw: Path, video: Path, src: dict) -> list[str]:
    if normalize_plan(src)["mode"] == "copy":
        return ["ffmpeg", "-y", "-i", str(raw), "-an", "-c:v", "copy", str(video)]
    return ["ffmpeg", "-y", "-i", str(raw),
            "-vf", f"scale=-2:{TARGET_HEIGHT}",   # ffmpeg applies the rotation flag first
            "-fps_mode", "cfr", "-r", str(int(FPS)),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",                # 10-bit/HDR phone video isn't decodable everywhere
            "-g", str(int(FPS)),                  # ~1 s GOP: clips cut with -c copy start where asked
            "-an", str(video)]


FRIENDLY_FFMPEG_ERRORS = (
    ("no video stream", "no readable video track — is this actually a video file?"),
    ("invalid data found", "video file is unreadable or corrupted — try re-exporting and re-uploading"),
    ("moov atom not found", "video file looks truncated (incomplete download/export) — try re-uploading"),
)


def _friendly_error(raw_error: str) -> str:
    low = raw_error.lower()
    for needle, friendly in FRIENDLY_FFMPEG_ERRORS:
        if needle in low:
            return friendly
    return raw_error


HIGHLIGHT_MAX_CLIPS = 5


def build_highlights(sdir: Path) -> Path | None:
    """Concat the top HIGHLIGHT_MAX_CLIPS rally clips (longest rallies first,
    by hit count) into one highlight reel. Returns the output path, or None
    if there are no rally clips to work with. Clips already share codec/params
    (all cut with `-c copy` from the same source video), so a concat-demuxer
    stream copy is safe and avoids a lossy re-encode."""
    ev_f = sdir / "events.json"
    if not ev_f.exists():
        return None
    rallies = load_json(ev_f).get("rallies", [])
    ranked = sorted(range(len(rallies)), key=lambda i: rallies[i]["hits"], reverse=True)
    clips = [sdir / "clips" / f"rally_{i:02d}.mp4" for i in ranked[:HIGHLIGHT_MAX_CLIPS]]
    clips = [c for c in clips if c.exists()]
    if not clips:
        return None
    out = sdir / "highlights.mp4"
    listfile = sdir / "highlights_list.txt"
    # concat-demuxer quoting: a literal ' inside '...' is written as '\''.
    # Without this any path containing an apostrophe (e.g. "Eric's MacBook")
    # truncates the filename and ffmpeg fails with "Impossible to open".
    listfile.write_text("\n".join(
        "file '" + str(c.resolve()).replace("'", "'\\''") + "'" for c in clips))
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
              "-c", "copy", str(out)])
    finally:
        listfile.unlink(missing_ok=True)
    return out


def ingest(sdir: Path, raw: Path) -> None:
    """Normalize video, extract audio (if present) + first frame.

    Fast path (see normalize_plan): an upload that is already 720p30 constant-rate
    H.264 is remuxed (-c copy) — near-instant. Anything else is re-encoded to that.
    """
    try:
        set_status(sdir, "ingest", "running")
        video = sdir / "video.mp4"
        src = probe_video(raw)
        if not src["codec"] or src["width"] == 0 or src["height"] == 0 or src["duration"] <= 0:
            msg = "video file is unreadable or corrupted — try re-exporting and re-uploading"
            set_status(sdir, "ingest", "error", msg)
            notify.notify_webhook(sdir, ok=False, error=msg, what="prepare the video")
            return
        plan = normalize_plan(src)
        log.info("ingest %s: %s%s", sdir.name, plan["mode"],
                 f" ({plan['reason']})" if plan["reason"] else "")
        _run(normalize_command(raw, video, src))
        if _has_audio(raw):  # audio only needed for hit detection
            _run(["ffmpeg", "-y", "-i", str(raw), "-vn", "-ac", "1", "-ar", "22050",
                  str(sdir / "audio.wav")])
        _run(["ffmpeg", "-y", "-i", str(video), "-vframes", "1", "-q:v", "3",
              str(sdir / "frame0.jpg")])
        atomic_write_json(sdir / "ingest.json", probe_video(video))
        set_status(sdir, "ingest", "done")
    except Exception as e:
        log.exception("ingest failed for %s", sdir.name)
        msg = _friendly_error(str(e))
        set_status(sdir, "ingest", "error", msg)
        notify.notify_webhook(sdir, ok=False, error=msg, what="prepare the video")
