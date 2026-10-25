"""Find recent podcast episodes with yt-dlp, without a YouTube API key."""

from datetime import datetime, timedelta, timezone
import json
import subprocess
from urllib.parse import urlencode, urlparse

from .common import VIDEO_ID, executable, run


QUERIES = {
    "India": ["Indian podcast full episode", "Hindi podcast full episode"],
    "US": ["American podcast full episode", "US podcast interview full episode"],
}


def channel_url(value):
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in ("youtube.com", "www.youtube.com")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"Use an HTTPS YouTube channel URL: {value}")
    path = parsed.path.rstrip("/")
    if path.endswith(("/videos", "/streams", "/shorts")):
        path = path.rsplit("/", 1)[0]
    if not (path.startswith("/@") or path.startswith("/channel/")):
        raise ValueError(f"Use a channel handle or /channel/ URL: {value}")
    return f"https://www.youtube.com{path}/videos"


def numbers(value):
    return value if type(value) in (int, float) and value >= 0 else 0


def discover(ctx, directory, seen):
    """Balance recent and relevance searches, then enrich a bounded shortlist."""
    tool = executable("yt-dlp")
    limit = ctx.cfg["search_results"]
    sources = []
    for market in ctx.cfg["markets"]:
        for query in QUERIES[market]:
            recent = "https://www.youtube.com/results?" + urlencode({"search_query": query, "sp": "EgIIBA=="})
            sources.extend([(market, recent),
                            (market, f"ytsearch{limit}:{query}")])
    sources.extend(("configured channel", channel_url(url)) for url in ctx.cfg["channels"])
    buckets, errors = [], []
    for index, (market, url) in enumerate(sources):
        print(f"Searching {market}: {url}", flush=True)
        try:
            output = run(ctx, [tool, "--ignore-config", "--flat-playlist", "--dump-single-json",
                               "--playlist-end", str(limit), "--socket-timeout", "20",
                               "--retries", "2", "--sleep-requests", "1", url],
                         timeout=180, log=directory / f"search-{index}.log")
            listing = json.loads(output)
            entries = [item for item in (listing.get("entries") or []) if isinstance(item, dict)]
            bucket = []
            for item in entries:
                video_id = item.get("id", "")
                if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id) or video_id in seen:
                    continue
                duration = numbers(item.get("duration"))
                if duration and not ctx.cfg["min_minutes"] * 60 <= duration <= ctx.cfg["max_minutes"] * 60:
                    continue
                if item.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                    continue
                bucket.append((market, item))
            buckets.append(bucket)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(ctx.clean(str(exc)))

    shortlist, selected = [], set()
    while any(buckets) and len(shortlist) < ctx.cfg["metadata_limit"]:
        for bucket in buckets:
            while bucket and bucket[0][1]["id"] in selected:
                bucket.pop(0)
            if bucket and len(shortlist) < ctx.cfg["metadata_limit"]:
                market, item = bucket.pop(0)
                selected.add(item["id"])
                shortlist.append((market, item))

    candidates = []
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=ctx.cfg["lookback_days"])).date()
    for market, item in shortlist:
        video_id = item["id"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            print(f"Reading episode details: {item.get('title', video_id)}", flush=True)
            info = json.loads(run(ctx, [tool, "--ignore-config", "--no-playlist", "--skip-download",
                                        "--dump-single-json", "--socket-timeout", "20", "--retries", "2", url],
                                  timeout=120, log=directory / f"metadata-{video_id}.log"))
            duration = numbers(info.get("duration"))
            if not ctx.cfg["min_minutes"] * 60 <= duration <= ctx.cfg["max_minutes"] * 60:
                continue
            if info.get("live_status") not in (None, "not_live", "was_live"):
                continue
            uploaded = datetime.strptime(info.get("upload_date", ""), "%Y%m%d").replace(tzinfo=timezone.utc)
            if uploaded.date() < cutoff or uploaded > now + timedelta(days=1):
                continue
            views = numbers(info.get("view_count"))
            age_hours = max(24, (now - uploaded).total_seconds() / 3600)
            candidates.append({
                "id": video_id, "url": url, "market_hint": market,
                "title": str(info.get("title", ""))[:300],
                "channel": str(info.get("channel", info.get("uploader", "")))[:200],
                "upload_date": info["upload_date"], "duration": duration,
                "views": views, "likes": numbers(info.get("like_count")),
                "views_per_day": round(views * 24 / age_hours),
                "description": str(info.get("description", ""))[:2000],
                "chapters": [{"title": str(c.get("title", ""))[:150], "start": c.get("start_time")}
                             for c in (info.get("chapters") or [])[:30]],
            })
        except Exception as exc:
            errors.append(f"{video_id}: {ctx.clean(str(exc))}")
    candidates.sort(key=lambda item: item["views_per_day"], reverse=True)
    return candidates, errors


def download(ctx, video, directory):
    """Download one validated YouTube ID into its own resumable directory."""
    target = directory / "source"
    target.mkdir(exist_ok=True)
    existing = list(target.glob("source.*"))
    for path in existing:
        if path.stem == "source" and path.suffix in (".mp4", ".mkv", ".webm") and path.stat().st_size:
            return path.resolve()
    run(ctx, [executable("yt-dlp"), "--ignore-config", "--no-playlist", "--socket-timeout", "30",
              "--retries", "3", "--fragment-retries", "3", "--max-filesize", "4G",
              "-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4",
              "--output", str(target / "source.%(ext)s"), video["url"]], timeout=3600,
        log=directory / "download.log")
    for path in target.glob("source.*"):
        if path.stem == "source" and path.suffix in (".mp4", ".mkv", ".webm") and path.stat().st_size:
            return path.resolve()
    raise RuntimeError("yt-dlp finished without a downloaded source video.")
