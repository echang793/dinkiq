"""Auto-highlight reel: top-N rally clips concatenated into one video."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline
from pipeline import SESSIONS


def _make_clip(path: Path, duration: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=64x64:d={duration}",
         "-c:v", "libx264", "-preset", "ultrafast", str(path)],
        capture_output=True, text=True, check=True)


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return float(out.stdout.strip())


def test_build_highlights_picks_top_n_by_hits_and_concats():
    sdir = SESSIONS / "hl_test_top_n"
    try:
        clips = sdir / "clips"
        clips.mkdir(parents=True)
        # 6 rallies, only top 5 by hit count should make the reel
        rallies = [{"start": i, "end": i + 0.5, "hits": h, "duration": 0.5}
                  for i, h in enumerate([2, 9, 4, 7, 3, 8])]
        (sdir / "events.json").write_text(json.dumps({"rallies": rallies}))
        for i in range(6):
            _make_clip(clips / f"rally_{i:02d}.mp4")

        out = pipeline.build_highlights(sdir)
        assert out is not None and out.exists()
        # 5 clips at 0.5s each concatenated -> ~2.5s (allow encode slop)
        dur = _probe_duration(out)
        assert 2.0 < dur < 3.2, dur
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


def test_build_highlights_none_without_events():
    sdir = SESSIONS / "hl_test_no_events"
    try:
        sdir.mkdir(parents=True)
        assert pipeline.build_highlights(sdir) is None
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


def test_build_highlights_none_without_clip_files():
    sdir = SESSIONS / "hl_test_no_clips"
    try:
        sdir.mkdir(parents=True)
        (sdir / "events.json").write_text(json.dumps(
            {"rallies": [{"start": 0, "end": 1, "hits": 5, "duration": 1}]}))
        # rally listed but clip file never written
        assert pipeline.build_highlights(sdir) is None
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


def test_clear_derived_removes_stale_highlights():
    sdir = SESSIONS / "hl_test_clear"
    try:
        sdir.mkdir(parents=True)
        (sdir / "highlights.mp4").write_bytes(b"stale")
        pipeline.clear_derived(sdir)
        assert not (sdir / "highlights.mp4").exists()
    finally:
        import shutil
        shutil.rmtree(sdir, ignore_errors=True)


if __name__ == "__main__":
    for fn in [test_build_highlights_picks_top_n_by_hits_and_concats,
               test_build_highlights_none_without_events,
               test_build_highlights_none_without_clip_files,
               test_clear_derived_removes_stale_highlights]:
        fn()
        print(f"ok {fn.__name__}")
