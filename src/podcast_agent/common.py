"""Configuration, local files, and subprocess execution."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tomllib


DEFAULTS = {
    "timezone": "",
    "markets": ["India", "US"], "clips_per_day": 3, "daily_time": "09:00",
    "lookback_days": 14, "min_minutes": 20, "max_minutes": 180,
    "search_results": 12, "metadata_limit": 24, "selection_candidates": 6,
    "channels": [], "codex_timeout_minutes": 15,
    "pipeline_timeout_minutes": 240, "source_retention_days": 7,
    "codex_model": "", "minimum_content_score": 3.5,
    "repair_attempts": 2,
    "output_dir": "data/reels", "telegram": False, "instagram": False,
    "instagram_times": ["10:00", "14:00", "18:00"],
}
KEYS = ("LLM_ENDPOINT", "LLM_MODEL", "LLM_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "INSTAGRAM_USER", "INSTAGRAM_PASS")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def load_env(path):
    """Read literal dotenv values without shell expansion or command execution."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def atomic_json(path, data):
    """Replace a JSON file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


class Context:
    """Keep the project configuration and credentials out of agent prompts."""

    def __init__(self, root, configured=True):
        self.root = Path(root).expanduser().resolve()
        config_path = self.root / "agent.toml"
        if configured and not config_path.exists():
            raise ValueError("Run ./setup.sh first to create agent.toml and .env.")
        overrides = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        unknown = set(overrides) - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"Unknown agent settings: {', '.join(sorted(unknown))}")
        self.cfg = DEFAULTS | overrides
        zone = self.cfg["timezone"]
        if not isinstance(zone, str):
            raise ValueError("timezone must be an IANA timezone name, such as Asia/Kolkata, or empty for laptop time.")
        try:
            self.timezone = ZoneInfo(zone) if zone else None
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown timezone: {zone}") from None
        if (not isinstance(self.cfg["markets"], list) or not self.cfg["markets"]
                or any(m not in ("India", "US") for m in self.cfg["markets"])):
            raise ValueError('markets must contain "India", "US", or both.')
        for key in ("clips_per_day", "lookback_days", "min_minutes", "max_minutes",
                    "search_results", "metadata_limit", "selection_candidates",
                    "codex_timeout_minutes", "pipeline_timeout_minutes", "source_retention_days"):
            if type(self.cfg[key]) is not int or self.cfg[key] <= 0:
                raise ValueError(f"{key} must be a positive integer.")
        if type(self.cfg["repair_attempts"]) is not int or self.cfg["repair_attempts"] < 0:
            raise ValueError("repair_attempts must be a nonnegative integer.")
        if self.cfg["clips_per_day"] > self.cfg["selection_candidates"]:
            raise ValueError("clips_per_day cannot exceed selection_candidates.")
        if self.cfg["min_minutes"] >= self.cfg["max_minutes"]:
            raise ValueError("min_minutes must be less than max_minutes.")
        if (not isinstance(self.cfg["daily_time"], str)
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.cfg["daily_time"])):
            raise ValueError("daily_time must use HH:MM, for example 09:00.")
        if not isinstance(self.cfg["codex_model"], str):
            raise ValueError("codex_model must be a model name string or omitted.")
        if not isinstance(self.cfg["output_dir"], str) or not self.cfg["output_dir"].strip():
            raise ValueError("output_dir must be a nonempty folder path.")
        if type(self.cfg["telegram"]) is not bool:
            raise ValueError("telegram must be true or false.")
        if type(self.cfg["instagram"]) is not bool:
            raise ValueError("instagram must be true or false.")
        times = self.cfg["instagram_times"]
        if (not isinstance(times, list) or not times or any(not isinstance(value, str)
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times)
                or len(set(times)) != len(times)):
            raise ValueError("instagram_times must contain distinct HH:MM times.")
        output = Path(self.cfg["output_dir"]).expanduser()
        self.output = (output if output.is_absolute() else self.root / output).resolve()
        if not isinstance(self.cfg["channels"], list) or any(not isinstance(v, str) for v in self.cfg["channels"]):
            raise ValueError("channels must be a list of YouTube channel URLs.")
        score = self.cfg["minimum_content_score"]
        if type(score) not in (int, float) or not 0 <= score <= 5:
            raise ValueError("minimum_content_score must be between 0 and 5.")
        self.secrets = load_env(self.root / ".env")
        self.secrets.update({key: os.environ[key] for key in KEYS if key in os.environ})
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)

    def now(self):
        """Return the current wall clock in the configured scheduling timezone."""
        return datetime.now(self.timezone)

    def slot(self, day, value):
        """Resolve an HH:MM scheduling slot to an unambiguous timestamp."""
        return datetime.combine(day, datetime.strptime(value, "%H:%M").time(), tzinfo=self.timezone)

    def clean(self, text):
        for key in ("LLM_API_KEY", "TELEGRAM_BOT_TOKEN", "INSTAGRAM_PASS"):
            value = self.secrets.get(key)
            if value:
                text = text.replace(value, "[REDACTED]")
        return text

    def env(self, podclipper=False, instagram=False):
        env = {key: value for key, value in os.environ.items()
               if key not in KEYS and not key.startswith(("INSTAGRAM_", "IG_"))
               and not key.endswith(("_API_KEY", "_BOT_TOKEN"))}
        if podclipper:
            env.update({key: value for key, value in self.secrets.items()
                        if key.startswith("LLM_") and value})
        if instagram:
            env.update({key: self.secrets[key] for key in ("INSTAGRAM_USER", "INSTAGRAM_PASS")
                        if self.secrets.get(key)})
        return env


def executable(name):
    result = shutil.which(name)
    if not result:
        raise ValueError(f"Missing {name}. Run ./setup.sh to install dependencies.")
    return result


def run(ctx, argv, timeout=180, log=None, cwd=None, input_text=None, podclipper=False, instagram=False):
    """Run an argument list without a shell and redact captured credentials."""
    process = subprocess.Popen(argv, cwd=cwd or ctx.root, env=ctx.env(podclipper, instagram),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, errors="replace", start_new_session=True)
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(ctx.clean(stdout + "\n" + stderr))
        raise
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(ctx.clean(stdout + "\n" + stderr))
    if process.returncode:
        detail = ctx.clean(stderr or stdout)[-1200:]
        raise RuntimeError(f"{Path(argv[0]).name} exited {process.returncode}: {detail}")
    return stdout


def day_directory(ctx):
    directory = ctx.data / "runs" / ctx.now().date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    return directory
