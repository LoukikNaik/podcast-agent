"""Command-line interface for setup, discovery, daily runs, and scheduling."""

import argparse
import os
from pathlib import Path
import sys

from .common import Context, load_env
from .onboarding import doctor, setup
from .pipeline import database, run_day
from .schedule import schedule
from .instagram import check_account, check_unresolved, login, publish_one, reschedule_posts, finish_actions


def main():
    parser = argparse.ArgumentParser(description="Find Indian and US podcasts and save reviewed reels to a local folder.")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project folder containing agent.toml and .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Configure PodClipper credentials (Telegram is optional)")
    commands.add_parser("doctor", help="Check local tools and configuration without network calls")
    commands.add_parser("discover", help="List recent episode candidates; do not call Codex, download media, or send messages")
    runner = commands.add_parser("run", help="Run or resume today's reel digest and enabled delivery channels")
    runner.add_argument("--resume", "--date", dest="date", help="Run or resume a dated folder, e.g. YYYY-MM-DD or YYYY-MM-DD-batch-01")
    runner.add_argument("--retry-uncertain-delivery", action="store_true", help="Retry ambiguous Telegram attempts after you have checked your chat")
    runner.add_argument("--scheduled-time", help="Run only within five minutes of HH:MM in the configured timezone")
    scheduler = commands.add_parser("schedule", help="Enable or remove daily macOS scheduling")
    scheduler.add_argument("--time", help="Daily HH:MM in the configured timezone")
    scheduler.add_argument("--remove", action="store_true")
    commands.add_parser("status", help="Show episode and delivery counts")
    resolve = commands.add_parser("resolve-delivery", help="Mark an ambiguous Telegram delivery as received")
    resolve.add_argument("--id", required=True)
    resolve.add_argument("--sent", required=True, action="store_true")
    instagram_login = commands.add_parser("instagram-login", help="Connect Instagram interactively; never uploads")
    instagram_login.add_argument("--from-podclipper", type=Path, help="Import Instagram credentials/session from an existing PodClipper checkout")
    commands.add_parser("instagram-check", help="Verify the connected Instagram account without posting")
    commands.add_parser("instagram-reschedule", help="Move remaining queued reels to future configured posting slots")
    commands.add_parser("instagram-followups", help="Finish or reconcile pending likes/reposts without uploading any reel")
    publisher = commands.add_parser("publish", help="Post one accepted reel from the Instagram queue")
    publisher.add_argument("--scheduled", action="store_true", help="Respect the current posting slot; never catch up in a burst")
    instagram_resolve = commands.add_parser("resolve-instagram", help="Resolve an ambiguous upload after checking Instagram")
    instagram_resolve.add_argument("--id", required=True, help="Attempt ID shown by status")
    resolution = instagram_resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--url", help="Existing reel URL: mark posted without uploading again")
    resolution.add_argument("--not-posted", action="store_true", help="Confirm nothing was published and permit retry")
    args = parser.parse_args()
    ctx = None
    try:
        if args.command == "setup":
            setup(args.project)
            return
        ctx = Context(args.project)
        if args.command == "doctor":
            doctor(ctx)
        elif args.command == "discover":
            run_day(ctx, discover_only=True)
        elif args.command == "run":
            run_day(ctx, day=args.date, retry_uncertain=args.retry_uncertain_delivery,
                    scheduled_time=args.scheduled_time)
        elif args.command == "schedule":
            if ctx.cfg["instagram"] and not args.remove:
                check_account(ctx)
                with database(ctx, lock_name="instagram-publish.lock") as db:
                    check_unresolved(ctx, db)
            schedule(ctx, args.time, args.remove)
        elif args.command == "instagram-login":
            login(ctx, args.from_podclipper)
        elif args.command == "instagram-check":
            check_account(ctx)
        elif args.command in ("instagram-reschedule", "instagram-followups"):
            with database(ctx, lock_name="instagram-publish.lock") as db:
                if args.command == "instagram-reschedule":
                    reschedule_posts(ctx, db)
                else:
                    finish_actions(ctx, db)
        elif args.command == "publish":
            publish_one(ctx, scheduled=args.scheduled)
        elif args.command in ("status", "resolve-delivery", "resolve-instagram"):
            lock_name = "run.lock" if args.command == "resolve-delivery" else "instagram-publish.lock"
            with database(ctx, lock_name=lock_name) as db:
                if args.command == "resolve-delivery":
                    result = db.execute("UPDATE deliveries SET state = 'sent' WHERE id = ? AND state != 'sent'", (args.id,))
                    db.commit()
                    print("Marked as received." if result.rowcount else "No unresolved delivery with that ID.")
                elif args.command == "resolve-instagram":
                    import re
                    if args.url and not re.fullmatch(r"https://www\.instagram\.com/reel/[A-Za-z0-9_-]+/", args.url):
                        raise ValueError("Use the full https://www.instagram.com/reel/CODE/ URL.")
                    account = ctx.secrets.get("INSTAGRAM_USER", "").casefold()
                    result = db.execute("UPDATE instagram_posts SET state = ?, url = ?, posted_at = "
                                        "CASE WHEN ? = 'posted' THEN attempted_at ELSE NULL END "
                                        "WHERE id = ? AND account = ? AND state = 'sending'",
                                        ("posted" if args.url else "ready", args.url,
                                         "posted" if args.url else "ready", args.id, account))
                    db.commit()
                    print("Resolved. Queued reels can continue at the next slot, or use ./run.sh publish." if result.rowcount else
                          "No unresolved attempt with that ID for the configured account.")
                else:
                    for row in db.execute("SELECT day, id, status FROM episodes ORDER BY day DESC LIMIT 15"):
                        print(" | ".join(row))
                    for state, count in db.execute("SELECT state, COUNT(*) FROM deliveries GROUP BY state"):
                        print(f"Telegram {state}: {count}")
                    for action, state, count in db.execute(
                            "SELECT a.action,a.state,COUNT(*) FROM instagram_actions a JOIN instagram_posts p ON p.id=a.post_id "
                            "WHERE p.account=? GROUP BY a.action,a.state", (ctx.secrets.get("INSTAGRAM_USER", "").casefold(),)):
                        print(f"Instagram {action} {state}: {count}")
                    print("Instagram slots: " + ", ".join(ctx.cfg["instagram_times"])
                          + " (" + (ctx.cfg["timezone"] or "laptop local time") + ")")
                    for row in db.execute("SELECT id, state, COALESCE(url, '') FROM instagram_posts ORDER BY COALESCE(attempted_at, queued_at) DESC LIMIT 20"):
                        print("Instagram " + " | ".join(row))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to resume.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        message = str(exc)
        if ctx:
            message = ctx.clean(message)
        else:
            values = load_env(args.project.expanduser().resolve() / ".env")
            for key in ("LLM_API_KEY", "TELEGRAM_BOT_TOKEN", "INSTAGRAM_PASS"):
                for value in (values.get(key), os.environ.get(key)):
                    if value:
                        message = message.replace(value, "[REDACTED]")
        print(f"Error: {message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
