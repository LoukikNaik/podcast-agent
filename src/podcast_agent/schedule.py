"""Per-user macOS scheduling for batch generation and spaced Instagram posts."""

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from collections import defaultdict
from fractions import Fraction


LABEL = "dev.podcast-agent.daily"
PUBLISH_LABEL = "dev.podcast-agent.instagram"


def interleave_sources(records):
    """Spread each source evenly through a batch while preserving its internal order."""
    groups = defaultdict(list)
    for record in records:
        groups[record['video']['id']].append(record)
    positions = [(Fraction(2 * index + 1, 2 * len(group)), source, index, record)
                 for source, group in groups.items() for index, record in enumerate(group)]
    return [record for _, _, _, record in sorted(positions)]


def mix_sources(records, fresh_ids=(), rng=None):
    """Randomize clips within sources and distribute new arrivals through the queue."""
    import random
    rng = rng or random.SystemRandom()
    groups = defaultdict(list)
    for record in records:
        groups[record['video']['id']].append(record)
    for group in groups.values():
        rng.shuffle(group)
    fresh = set(fresh_ids)
    choices = [r for group in groups.values() for r in group if r['id'] in fresh]
    first = rng.choice(choices) if choices else None
    if first is not None:
        groups[first['video']['id']].remove(first)
    positions = [(Fraction(2*i+1,2*len(group)),rng.random(),record)
                 for group in groups.values() for i,record in enumerate(group)]
    mixed = [record for _,_,record in sorted(positions,key=lambda item:(item[0],item[1]))]
    return ([first] if first is not None else [])+mixed


def schedule(ctx, at=None, remove=False):
    """Install or remove daily generation and posting slots without storing secrets."""
    if sys.platform != "darwin":
        raise ValueError("Automatic scheduling currently supports macOS. Run ./run.sh manually on other systems.")
    parent = Path.home() / "Library" / "LaunchAgents"
    domain = f"gui/{os.getuid()}"
    targets = {label: parent / f"{label}.plist" for label in (LABEL, PUBLISH_LABEL)}
    for target in targets.values():
        if target.exists() and plistlib.loads(target.read_bytes()).get("WorkingDirectory") != str(ctx.root):
            raise ValueError(f"A different project owns {target}; remove its schedule from that project first.")
    at = at or ctx.cfg["daily_time"]
    if not remove and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("Use HH:MM, for example ./run.sh schedule --time 09:00.")
    logs = ctx.data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    parent.mkdir(parents=True, exist_ok=True)
    environment = {"PATH": os.pathsep.join(dict.fromkeys([
        str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
        *os.environ.get("PATH", "").split(os.pathsep), "/usr/bin", "/bin"]))}
    if os.environ.get("CODEX_HOME"):
        environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
    for label, target in targets.items():
        if target.exists():
            subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True)
            target.unlink()
        if remove or (label == PUBLISH_LABEL and not ctx.cfg["instagram"]):
            continue
        posting = label == PUBLISH_LABEL
        times = ctx.cfg["instagram_times"] if posting else [at]
        intervals = [{"Hour": int(value[:2]), "Minute": int(value[3:])} for value in times]
        command = ["publish", "--scheduled"] if posting else ["run"]
        if ctx.timezone is not None and not posting:
            command += ["--scheduled-time", at]
        stem = "instagram" if posting else "schedule"
        config = {
            "Label": label,
            "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(ctx.root / "run.sh"), *command],
            "WorkingDirectory": str(ctx.root),
            "StandardOutPath": str(logs / f"{stem}.log"),
            "StandardErrorPath": str(logs / f"{stem}-error.log"),
            "EnvironmentVariables": environment, "ProcessType": "Standard", "RunAtLoad": False,
        }
        if ctx.timezone is not None:
            config["StartInterval"] = 60
        else:
            config["StartCalendarInterval"] = intervals if posting else intervals[0]
        target.write_bytes(plistlib.dumps(config))
        result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"Could not load {label}: {result.stderr.strip()}. Plist saved at {target}")
    if remove:
        print("Daily generation and Instagram posting schedules removed.")
        return
    zone = ctx.cfg["timezone"] or "your laptop's local time"
    print(f"Daily batch generation enabled at {at} in {zone}.")
    if ctx.cfg["instagram"]:
        print("Instagram: one queued reel at each slot: " + ", ".join(ctx.cfg["instagram_times"]) + f" ({zone}).")
    print("Your Mac must be on and you must be logged in. Idle sleep is prevented while a job runs.\n"
          f"Logs: {logs}\nDisable all schedules: ./run.sh schedule --remove")
