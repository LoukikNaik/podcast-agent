# Podcast Agent

**Run it on your laptop. Get podcast reels every day — saved locally or posted to Instagram.**

The agent finds recent, popular Indian and US podcast episodes across topics,
uses Codex to choose an episode, asks PodClipper to make vertical reels with
captions, and saves the clips that clear review.

**No YouTube Data API key. No hosted server. No Telegram account needed.**
Discovery and downloads use yt-dlp. Codex uses your CLI login. PodClipper uses
one endpoint, one model, and one API key.

## Set up once

On an **Apple Silicon Mac with macOS 14 or newer**, install
[Homebrew](https://brew.sh) if needed. Open this repository in Terminal:

```bash
./setup.sh
./run.sh
```

Setup installs missing tools, creates a private Python environment, and asks
for your PodClipper LLM settings. You can import an existing PodClipper `.env`.
It opens `codex login` if needed. Credentials stay in an ignored, private `.env`.
Setup does not download episodes, send messages, or enable scheduling.

## Find your reels

By default, reviewed reels appear in:

```text
data/reels/YYYY-MM-DD/
  <clip-id>.mp4     # Full-resolution vertical reel with captions
  <clip-id>.txt     # PodClipper scorecard
  <clip-id>.json    # Title, source, timestamps, transcript and review
  REPORT.md
  results.json
```

Defaults: **one episode per day, up to three reels**, chosen from both markets.
The agent may save fewer or none when candidates do not clear review. Rejected
exports and detailed logs remain in `data/runs/YYYY-MM-DD/`.

To save elsewhere, set `output_dir` in `agent.toml`. Relative paths use this
repository as their base; absolute paths and `~` also work:

```toml
markets = ["India", "US"]
clips_per_day = 3
output_dir = "~/Movies/Podcast Reels"
daily_time = "09:00"
```

For local drafts, check the complete reel before posting. Automatic review samples frames; it
cannot guarantee every frame, caption, or spoken claim is correct.

The first run downloads PodClipper's local models and can take a while. Long
episodes can take hours on CPU; keep the laptop connected to power and internet.

## Make it daily

After your first successful manual run:

```bash
./run.sh schedule                # 9 AM local time
./run.sh schedule --time 08:30
./run.sh schedule --remove
```

This uses a macOS LaunchAgent. Terminal does not need to stay open. You must be
logged in and the laptop must be on. Idle sleep is prevented during scheduled
processing; closing the lid can still suspend it. Calendar jobs missed during
sleep run after waking; jobs missed while powered off wait for the next time.
With an explicit timezone, generation and posting instead skip any missed
five-minute window; they do not catch up on waking.

Scheduling stores paths, never credentials. Logs are in `data/logs/`. Reinstall
the schedule after moving the repository or changing `daily_time`.

## Post to Instagram every day

Connect once, enable posting, and schedule it:

```bash
.venv/bin/python -m pip install -e '.[instagram]'
./run.sh instagram-login
```

Login asks for a username/password if needed and may ask for an Instagram
verification code. Credentials stay in `.env`; the session stays in ignored
`data/instagram/session.json`, both readable only by your user. You can import
an existing PodClipper checkout's credentials and session instead:

```bash
./run.sh instagram-login --from-podclipper '/path/to/agentic video editor'
```

Add one line to `agent.toml`, then enable the daily job:

```toml
instagram = true
```

For five posts spread through the day, use:

```toml
clips_per_day = 5
instagram_times = ["10:00", "12:00", "14:00", "16:00", "18:00"]
timezone = "Asia/Kolkata"
```

```bash
./run.sh instagram-check  # Check the actual destination account; no upload
./run.sh schedule         # 9 AM in the configured timezone
```

`timezone = "Asia/Kolkata"` makes generation, posting slots, run dates, and the
daily posting limit follow Indian Standard Time even if the laptop is in Chicago
or travels. Omit it or use an empty string for laptop local time. Reinstall the
schedule after changing the timezone. With an explicit timezone, macOS checks
once per minute and the app runs only inside the configured five-minute slot
window, independently of Chicago's daylight-saving changes.

At 9 AM in that timezone the agent processes one new episode and queues up to `clips_per_day`
accepted reels. A separate scheduled job posts **one normal reel** to
`INSTAGRAM_USER` at each `instagram_times` slot (default 10 AM, 2 PM, 6 PM).
Generation can take hours; only reels already in the queue can fill a slot.
The publisher runs independently of rendering, so it can post an existing
queued reel while the next batch is being prepared.
Captions and hashtags come from each reel's repaired second-pass transcript,
with a source credit and timestamped YouTube link. Captions are saved alongside
the video as `.caption.txt`. Generation reports record queue status; live URLs
are saved beside each reel in `.instagram.json` and shown by `./run.sh status`.

Instagram mode applies a public posting review instead of accepting cached
private draft reviews. Clips below `minimum_content_score` (default 3.5/5) or
with clear defects receive up to two targeted repairs (captions, crop, transcription or
word-aligned trimming), each followed by a fresh review. Editorial decisions also invalidate when their
prompt, metadata, frame sheets, schema or model setting changes. Clips still failing review are
skipped. One failed export does not block successful clips from being queued.
Normal transcription settings stay unchanged. If caption review rejects the
recognized words, a transcription repair selects a different source language,
Whisper task, VAD setting, or short vocabulary hint. Increasing beam size alone
or repeating the same profile is rejected. English translation is an explicit
fallback task, not a forced English source-language guess; translated word
timings still go through coverage checks and a fresh export review. Completed
crops and unrelated clips remain cached.
Some days may produce no posts. Frame sampling
still cannot verify the entire video. Completed batches are not posted
retroactively when you turn Instagram on.

Posting uses the same `instagrapi` client as PodClipper's `post-reel` skill,
with a pinned version and FFmpeg-generated covers. A saved session is reused;
daily jobs never wait for verification input. If Instagram expires the session,
run `./run.sh instagram-login` interactively, then resume the unfinished date.

Confirmed publication receipts are retained even if a later client telemetry or
metadata step fails. These successful uploads are never repeated.

Uploads are spaced at least 90 seconds apart. Posted clip IDs are remembered
per account. A timeout or ambiguous response blocks further uploads, including
on later days, until you check your Instagram profile and resolve the outcome:

```bash
./run.sh status
# It was posted: record the existing URL without re-uploading.
./run.sh resolve-instagram --id 'ATTEMPT_ID' --url 'https://www.instagram.com/reel/CODE/'
# OR: you checked the profile and confirmed it was NOT posted.
./run.sh resolve-instagram --id 'ATTEMPT_ID' --not-posted
./run.sh publish
```

Queue entries survive restarts. Missed slots never cause a burst of uploads:
the publisher sends at most one reel at the next slot, within the daily cap.
Empty queues skip the slot; remaining reels carry over to later days. To post
one queued reel immediately, use `./run.sh publish`. Reinstall the schedule
after changing `instagram_times`.

To stop posting, set `instagram = false`. To stop all scheduled jobs:
`./run.sh schedule --remove`. No Telegram configuration is needed.

## Useful commands

```bash
./run.sh doctor      # Local checks; no network or paid calls
./run.sh discover    # YouTube metadata only; no Codex or media downloads
./run.sh status      # Recent episode state (and Telegram state if enabled)
./run.sh setup       # Update credentials
./run.sh run --date 2026-10-11  # Resume a particular unfinished run
```

To process another episode after today's run is complete, use a new dated run
name. Reuse the same name to resume it after an interruption:

```bash
./run.sh run --resume 2026-10-15-batch-01
```

`--date` remains an alias for `--resume`. Daily scheduling still uses the plain
`YYYY-MM-DD` run automatically.

Repeated completed runs do nothing. Unfinished runs reuse completed downloads,
selections, matching reviews and exports. Completed crop and caption stages are reused
when their inputs match; caption-only repairs do not repeat the full transcription.
Export attempts and repair decisions are journaled under `recovery/<clip-id>/`. Output copies have stable clip IDs, so retries
do not create duplicate files. Changing the output folder affects future runs;
it does not move earlier batches.

Discovery writes `candidates.json`. A subsequent run reuses it. To refresh an
unstarted day's search, remove that day's `candidates.json` before running again.

## Optional settings

Defaults can be overridden in `agent.toml`; copying every setting is unnecessary:

```toml
lookback_days = 30       # Default 14
max_minutes = 120       # Default episode length range: 20–180 minutes
selection_candidates = 6
# channels = ["https://www.youtube.com/@your-favourite-podcast"]
# codex_model = "your-supported-codex-model"  # Otherwise Codex's built-in default
```

All defaults are in `src/podcast_agent/common.py`. Edit `podclipper.yaml` for
PodClipper options; it merges with PodClipper's packaged defaults. Set
`LLM_ENDPOINT`, `LLM_MODEL`, and `LLM_API_KEY` in `.env`.

## How it works

1. **yt-dlp** searches recent and relevance-ranked full episodes from India and
   the US, then fetches details for a bounded shortlist. “Top” means promising
   popular episodes among those results, not an exhaustive or official chart.
   Market search terms are hints, not proof of a creator's country.
2. **Codex CLI** chooses an episode using views, views/day, descriptions and
   chapters. It later reviews the actual selection transcripts and frame sheets.
   It runs read-only and returns structured decisions.
3. **PodClipper** transcribes, selects, crops, captions and exports through its
   existing CLI. This project does not copy its pipeline.
4. **Python** checks each export, samples its frames, and saves approved drafts
   to your output folder. Only one run can be active at a time.

Set `repair_attempts` in `agent.toml` to override the default of two repair attempts.
Retries are bounded and logged; unresolved failures remain in the report. Uploads with
uncertain outcomes still require checking Instagram before retrying.

Visual review includes six random frames, the beginning/end, and samples around
up to four large visual changes. Codex checks clipped faces, lost speakers,
unsuitable or duplicate panels, and title/caption issues. This can catch known
crop problems. The agent can retry with a single speaker crop, repair captions,
retry clip ASR, or trim a weak boundary; it does not rewrite the cropper.

Failures exit with an error and retain local logs. Completed jobs' raw downloads
and PodClipper caches are removed after seven days on a later successful run.
Reports, history and reels are kept; unfinished jobs are retained. Duration,
resolution, shortlist and timeout limits bound work, but are not a hard dollar
budget. Codex and PodClipper LLM usage follow their respective accounts.

## Telegram later (optional)

Local saving is the default and does not access Telegram. To also send approved
reels to your private chat, add this to `agent.toml`:

```toml
telegram = true
```

Then run `./run.sh setup`. Create a bot with `@BotFather` using `/newbot`, enter
its token into the local hidden prompt, open your bot and send `/start`. Setup
can detect your private chat. The token and chat ID stay in `.env`.

Telegram copies above 45 MB are compressed to 720 × 1280; local originals stay
full resolution. The agent records attempts before sending. If a connection
fails with an unknown outcome, check your chat before resolving or retrying:

```bash
./run.sh resolve-delivery --id 'DELIVERY_ID' --sent  # If it arrived
./run.sh run --date YYYY-MM-DD
# If it did not arrive:
./run.sh run --date YYYY-MM-DD --retry-uncertain-delivery
```

Enabling Telegram does not resend completed local batches. It applies to new
runs and unfinished jobs.

## Other laptops / manual installation

Automatic installation and scheduling target Apple Silicon macOS. Manual runs
support Unix with **Python 3.11–3.12**, FFmpeg/ffprobe, current yt-dlp and its
JavaScript components, Deno, Codex CLI, and PodClipper **0.1.4 or newer**:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . podclipper 'yt-dlp[default]'
./run.sh setup
./run.sh doctor
./run.sh
```

Install Codex, FFmpeg and Deno with their official installers/package managers.
Windows is not supported. Keep yt-dlp current if YouTube extraction changes.
Browser discovery is not implemented or required.

## References

- [Codex unattended runs](https://learn.chatgpt.com/docs/non-interactive-mode)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Why `ytsearchdate` is no longer used](https://github.com/yt-dlp/yt-dlp/issues/15898)
- [Telegram delivery](https://core.telegram.org/bots/api#sendvideo)
- [macOS scheduling and sleep](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)
- [PodClipper](https://github.com/LoukikNaik/PodClipper)
- [Instagram client sessions](https://subzeroid.github.io/instagrapi/latest/getting-started/)
- [Instagram Reel uploads](https://subzeroid.github.io/instagrapi/latest/usage-guide/media/)

## License

AGPL-3.0-only. PodClipper and other tools retain their own licenses.

## Running on an always-on server

Install the agent and PodClipper on the publishing machine. Keep credentials in
its ignored `.env` and authenticate with `./run.sh instagram-login`. Transfer
accepted reel files and their metadata to that machine before queueing them.
Enable the publisher on only one machine per Instagram account.

Configure posting times and timezone in `agent.toml`. The publisher sends one
queued reel per slot and skips missed slots without a burst. Keep the server on
and its macOS user logged in when using a LaunchAgent.

The publisher reuses its saved session. Only an explicit login-required response
triggers a password login. Network errors and rate limits do not. Failed logins
have a 15-minute cooldown; verification challenges require interactive login.

### Likes and native reposts after publishing

After confirming a reel was published, the Instagram publisher likes it and
uses instagrapi's `media_note_create()` to press the native two-arrow Repost
action. This shares the existing reel, with no additional upload or Story.
The agent uses the published instagrapi package; no fork is required.

The publication receipt is stored before either action. Likes and reposts have
separate durable records, and the server checks them on its normal publisher
polls, including outside posting slots. Completed actions are skipped. Known
preflight failures retry after five minutes, for at most three attempts. If a
request times out, the agent checks Instagram's current state instead of sending
another repost. An unresolved action stays visible in `./run.sh status` without
turning an already-posted reel back into an upload job.

`./run.sh instagram-followups` processes pending actions without uploading.
`./run.sh instagram-reschedule` assigns all remaining queued reels to future
configured slots, mixing source episodes while preserving the daily limit and posted history.
The exact plan is saved in `data/instagram/schedule.json`. Historical posts do
not receive retroactive likes or reposts merely by installing this update.

New accepted reels automatically remix the remaining posting queue. Clips are randomized
within each podcast and spread across upcoming slots, with a new arrival in the next slot.
Re-enqueuing an existing reel does not reshuffle it again. If publishing is in progress,
the mix is saved and applied after that upload. Posted reels stay unchanged, and the
existing two-hour cadence and publication checks still apply.
