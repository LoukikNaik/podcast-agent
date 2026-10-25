"""Isolated Instagram client with private sessions and noninteractive daily uploads."""

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

from .common import Context, atomic_json


class InstagramSetupError(RuntimeError):
    """An actionable local setup error that contains no credential values."""


def save_session(client, path):
    """Persist refreshed authentication atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(client.get_settings(), stream)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def connect(ctx, interactive=False):
    """Serialize session refresh while batch generation and posting run independently."""
    directory = ctx.data / "instagram"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "auth.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _connect(ctx, interactive)


def _connect(ctx, interactive=False):
    """Reuse valid sessions; renew expired authentication once with the saved password."""
    from instagrapi import Client
    from instagrapi.exceptions import ClientLoginRequired, LoginRequired, TwoFactorRequired
    logging.getLogger("instagrapi").setLevel(logging.CRITICAL)
    username = ctx.secrets.get("INSTAGRAM_USER", "")
    if not username:
        raise InstagramSetupError("Set INSTAGRAM_USER in .env first.")
    path = ctx.data / "instagram" / "session.json"
    attempt_path = path.parent / "reauth-attempt.json"
    client = Client()
    client.request_timeout = 30
    client.delay_range = [1, 3]

    def challenge(user, choice):
        if not interactive:
            raise InstagramSetupError("Instagram needs verification. Run ./run.sh instagram-login in Terminal.")
        return input(f"Instagram verification code for @{username}: ").strip()

    client.challenge_code_handler = challenge
    info = None
    if path.exists():
        client.load_settings(path, override_app_version=True)
        try:
            info = client.account_info()
        except (LoginRequired, ClientLoginRequired):
            pass
        except Exception:
            raise InstagramSetupError("Could not verify Instagram session; keeping it unchanged. Retry when Instagram is available.") from None
    elif not interactive:
        raise InstagramSetupError("Instagram session is missing. Run ./run.sh instagram-login.")
    if info is not None and info.username.casefold() != username.casefold():
        if not interactive:
            raise InstagramSetupError(f"Instagram session belongs to @{info.username}, expected @{username}. Upload blocked.")
        info = None
    if info is None:
        password = ctx.secrets.get("INSTAGRAM_PASS", "")
        if not password and interactive:
            import getpass
            password = getpass.getpass(f"Instagram password for @{username}: ")
        if not password:
            raise InstagramSetupError("Instagram session expired. Set INSTAGRAM_PASS or run ./run.sh instagram-login.")
        if not interactive:
            try:
                previous = json.loads(attempt_path.read_text())
            except FileNotFoundError:
                previous = {}
            if time.time() - previous.get("attempted_at", 0) < 900:
                raise InstagramSetupError("Recent Instagram login did not recover the session; waiting 15 minutes before another attempt. Use instagram-login if verification is needed.")
            atomic_json(attempt_path, {"attempted_at": time.time()})
            os.chmod(attempt_path, 0o600)
        try:
            client.login(username, password, relogin=True)
        except TwoFactorRequired:
            if not interactive:
                raise InstagramSetupError("Instagram requires a verification code. Run ./run.sh instagram-login in Terminal.") from None
            import getpass
            code = getpass.getpass("Instagram 2FA or backup code: ").strip()
            client.login(username, password, verification_code=code)
        info = client.account_info()
    if info.username.casefold() != username.casefold():
        raise InstagramSetupError(f"Instagram session belongs to @{info.username}, expected @{username}. Upload blocked.")
    save_session(client, path)
    attempt_path.unlink(missing_ok=True)
    return client, info


def media_receipt(media, user_id):
    """Accept only a concrete publication identity returned for this account."""
    if isinstance(media, dict):
        code = media.get("code", "")
        pk = str(media.get("pk") or media.get("id", "")).split("_")[0]
        owner = (media.get("user") or {}).get("pk")
    else:
        code = getattr(media, "code", "")
        pk = str(getattr(media, "pk", ""))
        owner = getattr(getattr(media, "user", None), "pk", None)
    if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code)
            or not pk.isdigit() or (owner is not None and str(owner) != str(user_id))):
        return None
    return {"pk": pk, "url": f"https://www.instagram.com/reel/{code}/"}


def upload(client, info, path, caption, thumbnail):
    """Retain the publish response if later library telemetry or parsing fails."""
    receipt = None
    configure = client.clip_configure

    def capture(*args, **kwargs):
        nonlocal receipt
        configured = configure(*args, **kwargs)
        if isinstance(configured, dict) and configured.get("status") == "ok":
            receipt = media_receipt(configured.get("media"), info.pk)
        return configured

    client.clip_configure = capture
    try:
        media = client.clip_upload(path=path, caption=caption, thumbnail=thumbnail, trial=False)
        receipt = receipt or media_receipt(media, info.pk)
    except Exception:
        if receipt is None:
            raise
        print("Instagram confirmed publication; using its receipt after a client follow-up error.", file=sys.stderr)
    finally:
        client.clip_configure = configure
    if receipt is None:
        raise RuntimeError("Instagram did not return a usable publication receipt.")
    return receipt


def engage(client, info, media_pk, action, perform=True):
    """Like/repost only verified own media; reconcile uncertain actions read-only."""
    result = {"media_pk": str(media_pk), "action": action, "state": "failed"}
    if action not in ("like", "repost") or not str(media_pk).isdigit():
        return result
    try:
        media = client.private_request(f"media/{media_pk}/info/")["items"][0]
    except Exception as exc:
        return result | {"state": "retry", "error": type(exc).__name__}
    if str(media.get("user", {}).get("pk")) != str(info.pk):
        return result | {"error": "OwnershipMismatch"}
    if str(media.get("pk")) != str(media_pk):
        return result | {"error": "MediaMismatch"}
    full_id = f"{media_pk}_{info.pk}"
    if action == "like" and media.get("has_liked") is True:
        return result | {"state": "done"}
    if action == "repost":
        for note in (media.get("media_notes") or {}).get("items", []):
            if (str(note.get("user_id")) == str(info.pk)
                    and str(note.get("media_id")) == str(media_pk)
                    and str(note.get("audience")) == "7" and str(note.get("note_style")) == "13"
                    and str(note.get("id", "")).isdigit()):
                return result | {"state": "done", "repost_id": str(note["id"])}
    if not perform:
        return result | {"state": "uncertain"}
    try:
        if action == "like":
            return result | {"state": "done" if client.media_like(full_id) else "retry"}
        # private_request otherwise resends timed-out/incomplete mutations internally.
        from instagrapi.exceptions import ClientIncompleteReadError, ClientRequestTimeout
        send = getattr(client, '_send_private_request', None)
        if send is not None:
            def single_attempt(*args, **kwargs):
                try:
                    return send(*args, **kwargs)
                except (ClientIncompleteReadError, ClientRequestTimeout):
                    raise RuntimeError('Repost outcome is uncertain') from None
            client._send_private_request = single_attempt
        try:
            receipt = client.media_note_create(full_id)
        finally:
            if send is not None:
                client._send_private_request = send
        if (receipt.get("status") == "ok" and str(receipt.get("id", "")).isdigit()
                and str(receipt.get("media_id", media_pk)) == str(media_pk)):
            return result | {"state": "done", "repost_id": str(receipt["id"])}
        return result | {"state": "uncertain", "error": "IncompleteRepostReceipt"}
    except Exception as exc:
        return result | {"state": "uncertain", "error": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("login", "check", "upload", "engage"))
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    ctx = Context(args.project)
    try:
        payload = json.load(sys.stdin) if args.operation == "engage" else None
        try:
            client, info = connect(ctx, interactive=args.operation == "login")
        except Exception as exc:
            if args.operation != "engage":
                raise
            print("PODCAST_INSTAGRAM_RESULT=" + json.dumps({
                "username": ctx.secrets.get("INSTAGRAM_USER", ""), "media_pk": str(payload.get("media_pk", "")),
                "action": payload.get("action"), "state": "retry", "error": type(exc).__name__}))
            return
        result = {"username": info.username, "user_id": str(info.pk)}
        if args.operation == "engage":
            client.request_timeout = 1
            result.update(engage(client, info, payload.get("media_pk"), payload.get("action"),
                                 perform=payload.get("perform") is True))
        if args.operation == "upload":
            payload = json.load(sys.stdin)
            path = Path(payload["path"]).resolve()
            if not path.is_relative_to(ctx.output) or path.suffix.lower() != ".mp4" or not path.is_file():
                raise ValueError("Only saved reels in the configured output folder can be uploaded.")
            result.update(upload(client, info, path, payload["caption"], Path(payload["thumbnail"])))
        print("PODCAST_INSTAGRAM_RESULT=" + json.dumps(result))
    except InstagramSetupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Instagram error ({type(exc).__name__}). Run ./run.sh instagram-login if authentication is needed.",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
