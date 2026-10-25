"""Private Telegram delivery with durable duplicate protection."""

import json
import secrets
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import executable, run


def call(ctx, method, fields=None, video=None):
    """Call the Bot API without placing the token in subprocess arguments."""
    token = ctx.secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN. Run ./run.sh setup.")
    fields = fields or {}
    headers = {}
    if video:
        boundary = "podcast-agent-" + secrets.token_hex(16)
        chunks = []
        for name, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="reel.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
        chunks.extend([video.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
        body = b"".join(chunks)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = urlencode(fields).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(f"https://api.telegram.org/bot{token}/{method}", data=body, headers=headers)
    try:
        with urlopen(request, timeout=180) as response:
            data = json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("description", "API request rejected")
        except (ValueError, UnicodeDecodeError):
            detail = "API request rejected"
        raise RuntimeError(f"Telegram {method}: {exc.code} {detail}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description', 'request failed')}")
    return data["result"]


def prepare_video(ctx, original, directory, duration):
    """Fit large delivery copies under the hosted Bot API upload limit."""
    if original.stat().st_size < 45_000_000:
        return original
    target = directory / "telegram.mp4"
    bitrate = int((42_000_000 * 8 / duration - 128_000) * 0.9)
    run(ctx, [executable("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(original),
              "-vf", "scale=720:1280", "-c:v", "libx264", "-preset", "medium",
              "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)],
        timeout=600, log=directory / "telegram-encode.log")
    if target.stat().st_size >= 45_000_000:
        raise RuntimeError("Delivery copy is still too large for Telegram; original is saved locally.")
    return target


def deliver(ctx, db, key, text, video=None, retry_uncertain=False):
    """Record each attempt before sending; require consent to retry uncertainty."""
    row = db.execute("SELECT state FROM deliveries WHERE id = ?", (key,)).fetchone()
    if row and row[0] == "sent":
        return
    if row and not retry_uncertain:
        raise RuntimeError(
            "A previous Telegram delivery has an unknown outcome. Check your chat, then use "
            "./run.sh resolve-delivery --id '" + key + "' --sent if it arrived, or "
            "./run.sh run --retry-uncertain-delivery if it did not."
        )
    db.execute("INSERT OR REPLACE INTO deliveries (id, state, message_id) VALUES (?, 'sending', NULL)", (key,))
    db.commit()
    fields = {"chat_id": ctx.secrets["TELEGRAM_CHAT_ID"]}
    fields.update({"caption": text[:900], "supports_streaming": "true"} if video else {"text": text[:4000]})
    result = call(ctx, "sendVideo" if video else "sendMessage", fields, video)
    db.execute("UPDATE deliveries SET state = 'sent', message_id = ? WHERE id = ?", (result["message_id"], key))
    db.commit()
