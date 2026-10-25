"""Media checks and reproducible visual samples for Codex review."""

import hashlib
import json
import math
import random
import re

from .common import atomic_json, executable, run


def inspect_media(ctx, path, directory):
    """Decode the export and check portrait video, audio, and duration."""
    info = json.loads(run(ctx, [executable("ffprobe"), "-v", "error", "-show_streams",
                                "-show_format", "-of", "json", str(path)], timeout=60))
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    duration = float(info["format"]["duration"])
    if (not video or not audio or video.get("codec_name") != "h264"
            or video.get("width") != 1080 or video.get("height") != 1920
            or not math.isfinite(duration) or not 0 < duration <= 91):
        raise ValueError(f"Invalid reel dimensions, streams, or duration: {path.name}")
    run(ctx, [executable("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", str(path),
              "-f", "null", "-"], timeout=300, log=directory / "decode.log")
    run(ctx, [executable("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path), "-vn",
              "-af", "volumedetect", "-f", "null", "-"], timeout=180, log=directory / "audio.log")
    text = (directory / "audio.log").read_text()
    if not re.search(r"max_volume: -?\d+(?:\.\d+)? dB", text):
        raise ValueError(f"Silent audio or unavailable volume measurement: {path.name}")
    result = {"duration": duration, "width": 1080, "height": 1920,
              "full_decode": True, "non_silent_audio": True}
    atomic_json(directory / "media.json", result)
    return result


def sample_frames(ctx, path, directory, duration):
    """Review random frames and neighborhoods of large visual changes."""
    from PIL import Image, ImageDraw, ImageFont

    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = executable("ffmpeg")
    scene_log = directory / "changes.log"
    run(ctx, [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-an", "-vf",
              "crop=iw:ih*0.43:0:ih*0.18,scale=160:-2,select='gt(scene,0.18)',metadata=print:key=lavfi.scene_score",
              "-f", "null", "-"], timeout=300, log=scene_log)
    events, timestamp = [], None
    for line in scene_log.read_text().splitlines():
        match = re.search(r"pts_time:([\d.]+)", line)
        if match:
            timestamp = float(match.group(1))
        match = re.search(r"lavfi.scene_score=([\d.]+)", line)
        if match and timestamp is not None:
            events.append({"time": timestamp, "score": float(match.group(1))})
    peaks = []
    for event in sorted(events, key=lambda e: e["score"], reverse=True):
        if all(abs(event["time"] - other["time"]) >= 1.5 for other in peaks):
            peaks.append(event)
        if len(peaks) == 4:
            break
    seed = int(hashlib.sha256(path.name.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    # Container duration can include audio after the last video frame.
    last_sample = max(0, duration - 0.5)
    samples = {0.0: "start", round(last_sample, 2): "ending"}
    for i in range(6):
        samples[round(min(last_sample, rng.uniform(i * duration / 6, (i + 1) * duration / 6)), 2)] = "random"
    for event in peaks:
        for offset, label in [(-0.24, "before change"), (0, "change"), (0.24, "after change")]:
            t = round(max(0, min(last_sample, event["time"] + offset)), 2)
            samples[t] = label
    frames = []
    for index, (t, label) in enumerate(sorted(samples.items())):
        target = directory / f"frame-{index:02d}-{t:06.2f}s.jpg"
        run(ctx, [ffmpeg, "-nostdin", "-v", "error", "-y", "-ss", str(t), "-i", str(path),
                  "-frames:v", "1", "-vf", "scale=540:960", "-pix_fmt", "yuvj420p",
                  "-q:v", "2", str(target)], timeout=60)
        frames.append({"time": t, "kind": label, "file": target.name})
    sheets = []
    font = ImageFont.load_default(size=16)
    for start in range(0, len(frames), 12):
        page = frames[start:start + 12]
        sheet = Image.new("RGB", (1080, math.ceil(len(page) / 4) * 520), "#222222")
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(page):
            x, y = (index % 4) * 270, (index // 4) * 520
            with Image.open(directory / item["file"]) as frame:
                sheet.paste(frame.resize((270, 480)), (x, y + 40))
            draw.text((x + 4, y + 3), f"{item['time']:.2f}s\n{item['kind']}", font=font, fill="white")
        target = directory / f"sheet-{len(sheets) + 1}.jpg"
        sheet.save(target, quality=92)
        sheets.append(target)
    atomic_json(directory / "samples.json", {
        "seed": seed, "frames": frames, "change_events": peaks,
        "method": "Six stratified random frames, start/end, and before/at/after up to four large changes. Change scoring excludes most title/caption area. Sampling is not full motion or caption-sync verification.",
    })
    return sheets
