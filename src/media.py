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


def probe_video(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True)
    except FileNotFoundError as e:
        raise _tool_missing("ffprobe") from e
    info = json.loads(proc.stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    duration = float(info.get("format", {}).get("duration") or 0.0)
    frames = int(st.get("nb_frames") or 0) or int(duration * fps)
    return {"codec": st.get("codec_name"), "width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0), "fps": round(fps, 3),
            "frames": frames, "duration": round(duration, 2)}


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

    Fast path: already-H.264, <=720p, ~30fps input is remuxed (-c copy)
    instead of re-encoded — near-instant for typical phone exports.
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
        if (src["codec"] == "h264" and src["height"] <= 720
                and 29.0 <= src["fps"] <= 31.0):
            _run(["ffmpeg", "-y", "-i", str(raw), "-an", "-c:v", "copy", str(video)])
        else:
            _run(["ffmpeg", "-y", "-i", str(raw),
                  "-vf", "scale=-2:720", "-r", str(int(FPS)),
                  "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", str(video)])
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
