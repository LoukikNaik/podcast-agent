"""Bounded per-clip export retries and repairs driven by public review."""

import hashlib
import json
import math
import re
from pathlib import Path

import yaml

from .common import atomic_json, executable, run
from .editor import decide, review_export, schema
from .instagram import clip_transcript
from .quality import inspect_media, sample_frames


def merge(base, changes):
    result = dict(base)
    for key, value in changes.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def repair_plan(ctx, directory, name, clip, export, review):
    """Choose one constrained repair; source cuts must use existing word boundaries."""
    sidecar = export.with_suffix(".captions.json")
    captions = json.loads(sidecar.read_text()) if sidecar.exists() else None
    state = json.loads((directory / "state.json").read_text())
    current = merge(yaml.safe_load((ctx.root / "podclipper.yaml").read_text()) or {}, state.get("overrides", {}))
    options = current.get("transcribe", {})
    second_pass = options.get("second_pass", {})
    current_profile = {"language": options.get("language") or "auto",
                       "task": second_pass.get("task", "transcribe"),
                       "vad_filter": options.get("vad_filter", True),
                       "initial_prompt": second_pass.get("initial_prompt") or "",
                       "beam_size": second_pass.get("beam_size", 5)}
    plan = decide(ctx, directory, name, (
        "Choose the smallest repair that addresses this rejection. Actions: captions retries "
        "mapped caption cleanup/romanization; crop rerenders a single tracked speaker instead "
        "of stacked panels; transcription retries only this clip's ASR; trim removes a weak "
        "opening/ending without changing speech; title fixes an unclear or misleading title "
        "using only what the caption transcript supports. skip means no credible repair. Do not simply "
        "repeat an unsuccessful repair in previous_attempts. For trim, start/end are ABSOLUTE "
        "SOURCE seconds computed from captions.source_start + word.start/end. Choose exact "
        "word boundaries, within the current clip, keeping 15–90 seconds and one complete "
        "thought. Never change caption text or invent speech. Without timed captions, trim "
        "is unavailable. For other actions keep the current source start/end unchanged. "
        "new_title must equal the current title unless action is title; use at most 8 words "
        "and 60 characters, with no invented names or claims. "
        "For transcription, provide a transcription_profile that materially differs from "
        "current_transcription and previous attempts: changing beam_size alone is insufficient. "
        "Use a supported Whisper source-language code or auto. For Hindi-English speech "
        "misrecognized through auto/Urdu detection, try language hi with task transcribe. "
        "If an explicit-language transcription already failed, task translate can produce "
        "English captions while keeping the actual source language, not language en. "
        "For English sources use relevant vocabulary hints or change VAD only if speech is "
        "being dropped. initial_prompt must be short, supported vocabulary context only, "
        "never guessed quotations, facts, dates or a replacement transcript. Keep VAD on "
        "unless the evidence suggests it removed speech. Do not give up on caption defects "
        "before trying a materially different transcription profile. For other actions, "
        "copy current_transcription into transcription_profile without applying it."
    ), {"clip": clip, "review": review, "captions": captions,
        "current_transcription": current_profile,
        "previous_attempts": state.get("repairs", [])}, schema({
        "action": {"type": "string", "enum": ["captions", "crop", "transcription", "trim", "title", "skip"]},
        "start": {"type": "number"}, "end": {"type": "number"}, "reason": {"type": "string"},
        "new_title": {"type": "string"},
        "transcription_profile": schema({
            "language": {"type": "string", "pattern": "^(auto|[a-z]{2,3})$"},
            "task": {"type": "string", "enum": ["transcribe", "translate"]},
            "vad_filter": {"type": "boolean"},
            "initial_prompt": {"type": "string", "maxLength": 300},
            "beam_size": {"type": "integer", "enum": [5, 8, 10]},
        }),
    }))
    if plan["action"] == "transcription":
        profile = plan["transcription_profile"]
        if (not isinstance(profile, dict) or not isinstance(profile.get("language"), str)
                or not re.fullmatch(r"auto|[a-z]{2,3}", profile["language"])
                or profile.get("task") not in ("transcribe", "translate")
                or type(profile.get("vad_filter")) is not bool
                or not isinstance(profile.get("initial_prompt"), str) or len(profile["initial_prompt"]) > 300
                or type(profile.get("beam_size")) is not int or profile["beam_size"] not in (5, 8, 10)):
            raise ValueError("Invalid transcription retry profile")
        meaningful = ("language", "task", "vad_filter", "initial_prompt")
        previous = [current_profile] + [attempt["transcription_profile"] for attempt in state.get("repairs", [])
                                        if attempt.get("action") == "transcription" and attempt.get("transcription_profile")]
        if any(all(profile[key] == old.get(key) for key in meaningful) for old in previous):
            raise ValueError("Transcription retry must change language, task, VAD, or vocabulary context")
    if plan["action"] == "title":
        title = plan.get("new_title")
        if not isinstance(title, str) or not title.strip() or len(title) > 60 or len(title.split()) > 8 or title == clip["title"]:
            raise ValueError("Invalid repair title.")
    if plan["action"] == "trim":
        start, end = plan["start"], plan["end"]
        if (not captions or any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
                or not clip["start"] <= start < end <= clip["end"] or not 15 <= end - start <= 90
                or (abs(start - clip["start"]) < 0.05 and abs(end - clip["end"]) < 0.05)):
            raise ValueError("Invalid repair trim bounds.")
        offset = captions["source_start"]
        starts = [offset + w["start"] for w in captions["words"]] + [clip["start"]]
        ends = [offset + w["end"] for w in captions["words"]] + [clip["end"]]
        if min(abs(start - t) for t in starts) > 0.03 or min(abs(end - t) for t in ends) > 0.03:
            raise ValueError("Repair trim does not follow existing word boundaries.")
    return plan


def prepare_clip(ctx, directory, video, original, export=None, *, source=None):
    """Resume one clip, repair rejected stages, and review every changed artifact anew."""
    from .pipeline import identity, output_clips

    if source is None:
        sources = [p for p in (directory / "source").iterdir()
                   if p.stem == "source" and p.suffix in (".mp4", ".mkv", ".webm")]
        if len(sources) != 1:
            raise ValueError("Expected one completed source download for clip recovery.")
        source = sources[0].resolve()

    clip_id = identity(video["id"], original)
    workspace = directory / "recovery" / clip_id
    workspace.mkdir(parents=True, exist_ok=True)
    state_path = workspace / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "clip": original, "overrides": {}, "repairs": [], "renders": 0,
    }
    repair_limit = state.get("repair_limit", ctx.cfg["repair_attempts"])
    if type(repair_limit) is not int or not 0 <= repair_limit <= 10:
        raise ValueError("Clip repair limit must be an integer from zero to ten")
    if state.get("export"):
        export = Path(state["export"])
        if not export.is_relative_to(directory) or not export.is_file():
            export = None
    elif state["repairs"]:
        export = None
    for cycle in range(repair_limit + 1):
        clip = state["clip"]
        if export is None:
            selection_path = workspace / "selection.json"
            atomic_json(selection_path, {"source": str(source), "clips": [clip]})
            while export is None and state["renders"] < repair_limit + 1:
                state["renders"] += 1
                atomic_json(state_path, state)
                config = yaml.safe_load((ctx.root / "podclipper.yaml").read_text()) or {}
                config = merge(config, state["overrides"])
                if state["renders"] > 1:
                    config = merge(config, {"subtitles": {"ffmpeg_preset": "veryfast"}})
                config_path = workspace / f"render-{state['renders']}.yaml"
                config_path.write_text(yaml.safe_dump(config))
                output = workspace / f"render-{state['renders']}"
                print(f"Exporting {clip['title']} (attempt {state['renders']})", flush=True)
                try:
                    run(ctx, [executable("podclipper"), str(source),
                              "-c", str(config_path), "--selection", str(selection_path), "--output-dir", str(output)],
                        timeout=ctx.cfg["pipeline_timeout_minutes"] * 60,
                        log=workspace / f"render-{state['renders']}.log", cwd=directory, podclipper=True)
                except Exception as exc:
                    state["last_error"] = ctx.clean(str(exc))[-2000:]
                    atomic_json(state_path, state)
                export = output_clips(output).get((round(clip["start"], 2), round(clip["end"], 2)))
                if export is None:
                    print(f"Export failed; completed stages remain cached: {clip['title']}", flush=True)
            if export is None:
                raise RuntimeError(f"Export retry limit reached for {clip['title']}; see {workspace}")
            state["export"] = str(export)
            atomic_json(state_path, state)

        transcript = clip_transcript(directory, export) if ctx.cfg["instagram"] else None
        digest = hashlib.sha256()
        with export.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(export.with_suffix(".txt").read_bytes())
        digest.update((transcript or "").encode())
        signature = digest.hexdigest()[:16]
        review_name = ("instagram-export-" if ctx.cfg["instagram"] else "export-") + clip_id + "-" + signature
        review_dir = workspace / review_name
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / "review.json"
        sheets = sorted(review_dir.glob("sheet-*.jpg"))
        if (review_dir / "media.json").exists() and sheets:
            media = json.loads((review_dir / "media.json").read_text())
        else:
            print(f"Reviewing {clip['title']}", flush=True)
            media = inspect_media(ctx, export, review_dir)
            sheets = sample_frames(ctx, export, review_dir, media["duration"])
        review = review_export(ctx, workspace, review_name, {
            "episode": video["title"], "clip": clip, "media": media,
            "scorecard": export.with_suffix(".txt").read_text(), "caption_transcript": transcript,
        }, sheets)
        atomic_json(review_path, review)
        state.update(export=str(export), review=review)
        atomic_json(state_path, state)
        if review["approve"] and review["content_score"] >= ctx.cfg["minimum_content_score"]:
            return clip, export, review, media, transcript
        if len(state["repairs"]) >= repair_limit or state.get("unrepairable"):
            return clip, export, review, media, transcript
        plan = repair_plan(ctx, workspace, f"repair-{len(state['repairs']) + 1}-{signature}", clip, export, review)
        if plan["action"] == "skip":
            state["unrepairable"] = True
            atomic_json(state_path, state)
            return clip, export, review, media, transcript
        state["repairs"].append(plan)
        overrides = state["overrides"]
        if plan["action"] == "crop":
            overrides = merge(overrides, {"crop": {"mode": "single"}, "diarize": {"enabled": False}})
        elif plan["action"] == "transcription":
            profile = plan["transcription_profile"]
            overrides = merge(overrides, {"transcribe": {
                "language": None if profile["language"] == "auto" else profile["language"],
                "vad_filter": profile["vad_filter"],
                "second_pass": {"beam_size": profile["beam_size"], "task": profile["task"],
                                "initial_prompt": profile["initial_prompt"] or None}}})
        elif plan["action"] == "trim":
            overrides = merge(overrides, {"clip_extract": {"pad_seconds": 0.0}})
            captions = json.loads(export.with_suffix(".captions.json").read_text())
            words = [w for w in captions["words"]
                     if w["start"] + captions["source_start"] >= plan["start"] - 0.03
                     and w["end"] + captions["source_start"] <= plan["end"] + 0.03]
            state["clip"] = clip | {
                "start": plan["start"], "end": plan["end"], "duration": plan["end"] - plan["start"],
                "reason": plan["reason"], "transcript": " ".join(w["text"] for w in words),
                "opening_5_seconds": " ".join(w["text"] for w in words if w["start"] + captions["source_start"] < plan["start"] + 5),
                "ending_7_seconds": " ".join(w["text"] for w in words if w["end"] + captions["source_start"] > plan["end"] - 7),
            }
        elif plan["action"] == "title":
            state["clip"] = clip | {"title": plan["new_title"].strip(), "reason": plan["reason"]}
        if plan["action"] in ("captions", "transcription"):
            overrides = merge(overrides, {"transcribe": {"cleanup": {"enabled": True, "transliterate": True,
                                                                   "max_attempts": 2 + len(state["repairs"])}}})
        state.update(overrides=overrides, export=None)
        atomic_json(state_path, state)
        print(f"Repairing {clip['title']}: {plan['action']} — {plan['reason']}", flush=True)
        if plan["action"] == "transcription":
            print(f"Transcription retry profile: {json.dumps(plan['transcription_profile'], ensure_ascii=False)}", flush=True)
        export = None
    raise RuntimeError(f"Repair loop exhausted for {original['title']}")
