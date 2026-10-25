"""Resume a bounded daily discovery, selection, export, and delivery job."""

from contextlib import contextmanager
from datetime import date, timedelta
import fcntl
import hashlib
import json
import re
from pathlib import Path
import shutil
import sqlite3

from .common import atomic_json, executable, run
from .discovery import discover, download
from .editor import choose_episode, review_export, review_selection
from .quality import inspect_media, sample_frames
from .recovery import prepare_clip
from .instagram import caption_for, check_account, check_unresolved, clip_transcript, enqueue
from .telegram import deliver, prepare_video


@contextmanager
def database(ctx, lock_name="run.lock"):
    """Prevent overlapping runs and retain episode/delivery history."""
    with (ctx.data / lock_name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another podcast-agent command is already running.") from None
        with sqlite3.connect(ctx.data / "history.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, day TEXT, status TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, state TEXT, message_id INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS instagram_posts (id TEXT PRIMARY KEY, account TEXT, "
                       "clip_id TEXT, state TEXT, caption TEXT, url TEXT, media_pk TEXT, attempted_at REAL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(instagram_posts)")}
            for column, kind in (("path", "TEXT"), ("queued_at", "REAL"), ("posted_at", "REAL"), ("scheduled_at", "REAL")):
                if column not in columns:
                    db.execute(f"ALTER TABLE instagram_posts ADD COLUMN {column} {kind}")
            db.execute("CREATE TABLE IF NOT EXISTS instagram_actions ("
                       "post_id TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
                       "attempts INTEGER NOT NULL DEFAULT 0, attempted_at REAL, receipt TEXT, error TEXT, "
                       "PRIMARY KEY(post_id,action))")
            db.execute("CREATE TABLE IF NOT EXISTS instagram_queue_updates (post_id TEXT PRIMARY KEY)")
            db.commit()
            yield db


def identity(video_id, clip):
    return hashlib.sha256(f"{video_id}:{clip['start']:.2f}:{clip['end']:.2f}".encode()).hexdigest()[:16]


def output_clips(directory):
    result = {}
    for path in sorted(directory.glob("*/reel_*.mp4")):
        sidecar = path.with_suffix(".txt")
        if not sidecar.exists():
            continue
        fields = {}
        for line in sidecar.read_text().splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        try:
            bounds = (round(float(fields["source_start"]), 2), round(float(fields["source_end"]), 2))
            result[bounds] = path
        except (KeyError, ValueError):
            continue
    return result


def save_reel(ctx, day, clip_id, export, clip, video, review):
    """Keep accepted full-resolution drafts in a stable, configurable folder."""
    target = ctx.output / day
    target.mkdir(parents=True, exist_ok=True)
    saved = target / f"{clip_id}.mp4"
    temporary = saved.with_suffix(".mp4.tmp")
    shutil.copyfile(export, temporary)
    temporary.replace(saved)
    shutil.copyfile(export.with_suffix(".txt"), saved.with_suffix(".txt"))
    if export.with_suffix(".captions.json").exists():
        shutil.copyfile(export.with_suffix(".captions.json"), saved.with_suffix(".captions.json"))
    atomic_json(saved.with_suffix(".json"), {"episode": video, "clip": clip, "review": review})
    return saved


def run_day(ctx, day=None, retry_uncertain=False, discover_only=False, scheduled_time=None):
    """Run or resume one day's episode; repeated completed runs do nothing."""
    now = ctx.now()
    if scheduled_time is not None:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", scheduled_time):
            raise ValueError("Scheduled generation time must use HH:MM.")
        age = now.timestamp() - ctx.slot(now.date(), scheduled_time).timestamp()
        if not 0 <= age < 300:
            print("No daily generation slot is due.")
            return
    run_id = day or now.date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,60})?", run_id):
        raise ValueError("Use a run date or run folder name, such as 2026-10-15-batch-01.")
    day = run_id[:10]
    date.fromisoformat(day)
    directory = ctx.data / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Run folder: {directory}", flush=True)
    with database(ctx) as db:
        if discover_only:
            seen = {row[0] for row in db.execute("SELECT id FROM episodes")}
            candidates, errors = discover(ctx, directory / "discovery", seen)
            atomic_json(directory / "candidates.json", {"candidates": candidates, "errors": errors})
            print(f"Found {len(candidates)} candidates. Saved to {directory / 'candidates.json'}")
            return
        required = ["LLM_API_KEY", "LLM_ENDPOINT", "LLM_MODEL"]
        if ctx.cfg["telegram"]:
            required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
        for key in required:
            if not ctx.secrets.get(key):
                raise ValueError(f"Missing {key}. Run ./run.sh setup.")
        job_path = directory / "job.json"
        job = json.loads(job_path.read_text()) if job_path.exists() else {"day": day, "done": False}
        if job.get("done"):
            print(f"{run_id} already completed. See {directory / 'REPORT.md'}")
            return
        if ctx.cfg["instagram"]:
            check_unresolved(ctx, db)
            check_account(ctx)
        try:
            process(ctx, db, directory, job, retry_uncertain)
        except Exception as exc:
            atomic_json(directory / "error.json", {"error": ctx.clean(str(exc)), "resume": f"./run.sh run --resume {run_id}"})
            raise


def process(ctx, db, directory, job, retry_uncertain):
    job_path = directory / "job.json"
    if "episode" not in job:
        candidate_path = directory / "candidates.json"
        if candidate_path.exists():
            cached = json.loads(candidate_path.read_text())
            candidates = cached["candidates"]
            if not candidates and cached.get("errors"):
                candidate_path.unlink()
                raise RuntimeError("Previous discovery failed. The failed cache was cleared; run again to refresh discovery.")
            seen = {row[0] for row in db.execute("SELECT id FROM episodes")}
            candidates = [item for item in candidates if item["id"] not in seen]
        else:
            seen = {row[0] for row in db.execute("SELECT id FROM episodes")}
            candidates, errors = discover(ctx, directory / "discovery", seen)
            atomic_json(candidate_path, {"candidates": candidates, "errors": errors})
            if not candidates and errors:
                raise RuntimeError("Discovery found no usable candidates and had errors. See candidates.json and discovery logs; retry later.")
        video, reason = choose_episode(ctx, directory, candidates)
        job.update(episode=video, episode_reason=reason)
        atomic_json(job_path, job)
        if video:
            db.execute("INSERT OR REPLACE INTO episodes VALUES (?, ?, 'processing')", (video["id"], job["day"]))
            db.commit()
    video = job["episode"]
    records = []
    if video:
        db.execute("INSERT OR IGNORE INTO episodes VALUES (?, ?, 'processing')", (video["id"], job["day"]))
        db.commit()
        print(f"Episode: {video['title']}\n{video['url']}\n{job['episode_reason']}", flush=True)
        source = download(ctx, video, directory)
        selection_root = directory / "selection"
        selection_files = sorted(selection_root.glob("*/selection.json"))
        if not selection_files:
            print("PodClipper: transcribing and selecting candidate reels...", flush=True)
            run(ctx, [executable("podclipper"), str(source), "-c", str(ctx.root / "podclipper.yaml"),
                      "--clips", str(ctx.cfg["selection_candidates"]), "--select-only", "--output-dir", str(selection_root)],
                timeout=ctx.cfg["pipeline_timeout_minutes"] * 60, log=directory / "selection.log",
                cwd=directory, podclipper=True)
            selection_files = sorted(selection_root.glob("*/selection.json"))
        if not selection_files:
            raise RuntimeError("PodClipper did not produce selection.json. See selection.log.")
        selection = json.loads(selection_files[-1].read_text())
        approved_path = directory / "approved-selection.json"
        if approved_path.exists():
            approved = json.loads(approved_path.read_text())
        else:
            approved = {"source": str(source), "clips": review_selection(ctx, directory, selection)}
            atomic_json(approved_path, approved)
        export_root = directory / "exports"
        existing = output_clips(export_root)
        ordered = sorted(approved["clips"], key=lambda c: (round(c["start"], 2), round(c["end"], 2)) not in existing)
        for original in ordered:
            clip_id = identity(video["id"], original)
            initial = existing.get((round(original["start"], 2), round(original["end"], 2)))
            try:
                clip, export, review, media, transcript = prepare_clip(ctx, directory, video, original, initial, source=source)
            except Exception as exc:
                error = ctx.clean(str(exc))[-2000:]
                print(f"Clip failed after retries: {original['title']}: {error}", flush=True)
                records.append({"title": original["title"], "accepted": False, "clip_id": clip_id,
                                "path": str(initial) if initial else "", "error": error,
                                "review": {"approve": False, "content_score": 0,
                                           "reason": "Processing failed after bounded retries", "issues": [error]}})
                atomic_json(directory / "results.json", records)
                continue
            review_dir = directory / "recovery" / clip_id
            acceptable = review["approve"] and review["content_score"] >= ctx.cfg["minimum_content_score"]
            records.append({"title": clip["title"], "path": str(export), "accepted": acceptable,
                            "review": review, "clip_id": clip_id})
            try:
                if acceptable:
                    saved = save_reel(ctx, job["day"], clip_id, export, clip, video, review)
                    records[-1]["saved_path"] = str(saved)
                    print(f"Saved: {saved}", flush=True)
                atomic_json(directory / "results.json", records)
                if acceptable and ctx.cfg["instagram"]:
                    caption = caption_for(ctx, directory, clip_id, clip, video, transcript)
                    saved.with_suffix(".caption.txt").write_text(caption + "\n")
                    queued = enqueue(ctx, db, clip_id, saved, caption)
                    records[-1]["instagram_state"] = queued["state"]
                    if queued.get("url"):
                        records[-1]["instagram_url"] = queued["url"]
                    atomic_json(saved.with_suffix(".instagram.json"), queued)
                    atomic_json(directory / "results.json", records)
                if acceptable and ctx.cfg["telegram"]:
                    caption = (f"Review draft: {clip['title']}\n\n{review['reason'][:450]}\n\n"
                               f"Source: {video['url']}&t={int(clip['start'])}s\n"
                               "Please review the full clip before posting.")
                    delivery_copy = prepare_video(ctx, export, review_dir, media["duration"])
                    deliver(ctx, db, f"clip:{clip_id}:{ctx.secrets['TELEGRAM_CHAT_ID']}", caption,
                            video=delivery_copy, retry_uncertain=retry_uncertain)
            except Exception as exc:
                records[-1]["error"] = ctx.clean(str(exc))[-2000:]
                print(f"Delivery preparation failed for {clip['title']}: {records[-1]['error']}", flush=True)
                atomic_json(directory / "results.json", records)
    accepted = sum(record["accepted"] for record in records)
    lines = [f"# Podcast Agent — {job['day']}", "", f"Episode: {video['title'] if video else 'None'}",
             f"Source: {video['url'] if video else 'None'}", "", job["episode_reason"], "",
             f"Reels saved: {accepted}/{len(records)} reviewed.",
             f"Folder: {ctx.output / job['day']}", "",
             ("Instagram mode: accepted reels are queued for scheduled posting. " if ctx.cfg["instagram"] else
              "These are private review drafts. ") +
             "Frame sampling does not verify every frame, motion, or caption timing.", ""]
    for record in records:
        lines.extend([f"## {record['title']}", "", f"{'Saved' if record['accepted'] else 'Skipped'}: {record['review']['reason']}",
                      f"Content: {record['review']['content_score']}/5", f"File: {record.get('saved_path', record['path'])}",
                      f"Issues: {'; '.join(record['review']['issues']) or 'None observed in samples'}", ""])
        if record.get("instagram_url"):
            lines.extend([f"Instagram: {record['instagram_url']}", ""])
        elif record.get("instagram_state"):
            lines.extend([f"Instagram: {record['instagram_state']}; live receipt will be saved beside the reel.", ""])
        if record.get("error"):
            lines.extend([f"Processing error: {record['error']}", ""])
    (directory / "REPORT.md").write_text("\n".join(lines))
    destination = ctx.output / job["day"]
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(directory / "REPORT.md", destination / "REPORT.md")
    atomic_json(destination / "results.json", records)
    summary = (f"Podcast Agent — {job['day']}\n{accepted} reels sent; {len(records) - accepted} skipped.\n"
               + (f"{video['title']}\n{video['url']}" if video else job["episode_reason"]))
    if not accepted and records:
        summary += "\n\nNo clips cleared review: " + records[0]["review"]["reason"][:500]
    if ctx.cfg["telegram"]:
        deliver(ctx, db, f"summary:{job['day']}:{ctx.secrets['TELEGRAM_CHAT_ID']}", summary,
                retry_uncertain=retry_uncertain)
    job["done"] = not any(record.get("error") for record in records)
    atomic_json(job_path, job)
    if video:
        db.execute("UPDATE episodes SET status = ? WHERE id = ?",
                   ("done" if job["done"] else "partial", video["id"]))
        db.commit()
    status = "Finished" if job["done"] else "Partial batch"
    print(f"{status}: {accepted} reels saved to {destination}. Report: {destination / 'REPORT.md'}")
    if job["done"]:
        (directory / "error.json").unlink(missing_ok=True)
        cleanup_sources(ctx)
    else:
        atomic_json(directory / "error.json", {"error": "Some clips need attention after bounded retries",
                                              "clips": [r["clip_id"] for r in records if r.get("error")]})


def cleanup_sources(ctx):
    """Remove only old completed jobs' source downloads and PodClipper caches."""
    cutoff = ctx.now().date() - timedelta(days=ctx.cfg["source_retention_days"])
    for path in (ctx.data / "runs").glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
            expired = job.get("done") and date.fromisoformat(job["day"]) < cutoff
        except (ValueError, KeyError):
            continue
        if expired:
            for name in ("source", ".cache"):
                target = path.parent / name
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
