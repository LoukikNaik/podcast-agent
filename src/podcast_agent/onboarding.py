"""Interactive local setup and dependency checks."""

import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess

from .common import Context, KEYS, executable, load_env, run
from .telegram import call


def prompt(label, default="", secret=False):
    suffix = " [keep existing]" if secret and default else f" [{default}]" if default else ""
    value = (getpass.getpass if secret else input)(label + suffix + ": ").strip()
    return value or default


def save_credentials(root, values):
    """Persist setup progress privately so failed connections do not lose input."""
    target = root / ".env"
    temporary = target.with_suffix(".env.tmp")
    selected = {key: str(values.get(key, "")) for key in KEYS}
    if any("\n" in value or "\r" in value for value in selected.values()):
        raise ValueError("Credential values must be a single line.")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("# Private local credentials. Never commit this file.\n")
        stream.write("\n".join(f"{key}={value}" for key, value in selected.items()) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def setup(root):
    """Create small configs and collect credentials privately on the laptop."""
    ctx = Context(root, configured=False)
    example = ctx.root / "agent.example.toml"
    config = ctx.root / "agent.toml"
    if not config.exists():
        config.write_text(example.read_text())
    values = load_env(ctx.root / ".env")
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    print("Daily defaults: popular Indian + US podcasts, one episode, up to three reels.\n")
    if not values.get("LLM_API_KEY"):
        existing = prompt("Existing PodClipper .env path (optional; Enter to skip)")
        if existing:
            existing_path = Path(existing).expanduser()
            if not existing_path.is_file():
                raise ValueError("That PodClipper .env file does not exist.")
            imported = load_env(existing_path)
            values.update({key: value for key, value in imported.items() if key.startswith("LLM_")})
    print("PodClipper uses one endpoint, model, and API key. Codex uses `codex login`. ")
    values["LLM_ENDPOINT"] = prompt("LLM endpoint", values.get("LLM_ENDPOINT", "https://openrouter.ai/api/v1"))
    values["LLM_MODEL"] = prompt("LLM model", values.get("LLM_MODEL", "z-ai/glm-5.3-flash"))
    values["LLM_API_KEY"] = prompt("LLM API key", values.get("LLM_API_KEY", ""), secret=True)
    if not values["LLM_API_KEY"]:
        raise ValueError("Add your LLM API key to complete setup.")
    save_credentials(ctx.root, values)
    if ctx.cfg["telegram"]:
        setup_telegram(ctx, values)
    if ctx.cfg["instagram"]:
        from .instagram import login
        import sys
        run(ctx, [sys.executable, "-m", "pip", "install", "-e", ".[instagram]"], timeout=300)
        ctx.secrets = values
        login(ctx)
    pod_config = ctx.root / "podclipper.yaml"
    if not pod_config.exists():
        pod_config.write_text("content:\n  clips: 6\noutput:\n  captions: classic\n")
    auth = subprocess.run([executable("codex"), "login", "status"], capture_output=True, text=True)
    if auth.returncode:
        print("\nSign in to Codex once:")
        subprocess.run([executable("codex"), "login"], check=True)
    print(f"\nSetup saved. Reels will be saved to {ctx.output}.\n"
          "No videos downloaded, messages sent, or schedule enabled.\n"
          "Run now: ./run.sh\nEnable daily runs later: ./run.sh schedule")


def setup_telegram(ctx, values):
    """Connect Telegram only when explicitly enabled in configuration."""
    print("\nTelegram: create a bot with @BotFather, then paste its token here (hidden).")
    values["TELEGRAM_BOT_TOKEN"] = prompt("Telegram bot token", values.get("TELEGRAM_BOT_TOKEN", ""), secret=True)
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", values["TELEGRAM_BOT_TOKEN"]):
        raise ValueError("The Telegram bot token does not look valid.")
    save_credentials(ctx.root, values)
    ctx.secrets = values
    me = call(ctx, "getMe")
    print(f"Open https://t.me/{me['username']} and send /start.")
    chat_id = prompt("Telegram chat ID (Enter to detect)", values.get("TELEGRAM_CHAT_ID", ""))
    if not chat_id:
        input("After sending /start to your bot, press Enter here.")
        updates = call(ctx, "getUpdates", {"timeout": "0", "limit": "100"})
        chats = {}
        for update in updates:
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") == "private":
                chats[str(chat["id"])] = chat.get("username") or chat.get("first_name", "Private chat")
        if not chats:
            raise ValueError("No private chat found. Send /start to this bot, then rerun ./run.sh setup.")
        choices = list(chats)
        for i, item in enumerate(choices, 1):
            print(f"{i}. {chats[item]} (chat ID {item})")
        choice = prompt("Choose your chat number", "1")
        if not choice.isdigit() or not 1 <= int(choice) <= len(choices):
            raise ValueError("Invalid chat number.")
        chat_id = choices[int(choice) - 1]
    if not re.fullmatch(r"-?\d+", chat_id):
        raise ValueError("Telegram chat ID must be numeric.")
    values["TELEGRAM_CHAT_ID"] = chat_id
    save_credentials(ctx.root, values)


def doctor(ctx):
    """Check local prerequisites without downloads, paid calls, or messages."""
    missing = []
    for name in ("codex", "yt-dlp", "podclipper", "ffmpeg", "ffprobe", "deno"):
        path = shutil.which(name)
        print(f"{'OK' if path else 'MISSING'} {name}: {path or 'run ./setup.sh'}")
        if not path:
            missing.append(name)
        elif name in ("ffmpeg", "ffprobe", "deno", "yt-dlp"):
            try:
                flag = "-version" if name in ("ffmpeg", "ffprobe") else "--version"
                run(ctx, [path, flag], timeout=30)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"BROKEN {name}: {ctx.clean(str(exc))}")
                missing.append(f"working {name}")
    required = list(KEYS[:3])
    if ctx.cfg["telegram"]:
        required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    for key in required:
        available = bool(ctx.secrets.get(key))
        print(f"{'OK' if available else 'MISSING'} {key}")
        if not available:
            missing.append(key)
    if shutil.which("codex"):
        help_text = run(ctx, [executable("codex"), "exec", "--help"], timeout=30)
        for flag in ("--ignore-user-config", "--output-schema", "--image", "--ephemeral"):
            if flag not in help_text:
                missing.append(f"Codex support for {flag}; update Codex CLI")
        result = subprocess.run([executable("codex"), "login", "status"], capture_output=True, text=True)
        print(f"{'OK' if result.returncode == 0 else 'MISSING'} Codex login")
        if result.returncode:
            missing.append("Codex login; run codex login")
    if shutil.which("podclipper"):
        version = run(ctx, [executable("podclipper"), "--version"], timeout=30)
        numbers = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if not numbers or tuple(map(int, numbers.groups())) < (0, 1, 4):
            missing.append("PodClipper >=0.1.4 recovery support (use the matching development build until released)")
        help_text = run(ctx, [executable("podclipper"), "--help"], timeout=30)
        if not all(flag in help_text for flag in ("--select-only", "--selection")):
            missing.append("PodClipper selection support; install PodClipper >=0.1.1")
    if ctx.cfg["instagram"]:
        from .instagram import require_instagram
        try:
            require_instagram(ctx)
            print("OK Instagram support and saved session (use instagram-check to verify online)")
        except ValueError as exc:
            missing.append(str(exc))
    if missing:
        raise ValueError("Setup incomplete: " + ", ".join(missing))
    print("Local prerequisites are ready. Network access and actual processing have not been checked.")
