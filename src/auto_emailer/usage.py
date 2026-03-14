"""API usage logging and reporting.

Events are appended as JSON lines to ~/.local/share/auto-emailer/usage.jsonl.
Each line: {"ts": "2026-03-14T08:05:11", "type": "gemini"|"serper"|"jina", ...}
"""
import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

USAGE_FILE = Path.home() / ".local" / "share" / "auto-emailer" / "usage.jsonl"


def log_usage(event_type: str, **kwargs) -> None:
    """Append a usage event to the JSONL log. Silent on any failure."""
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "type": event_type, **kwargs}
        with USAGE_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def print_report(month: str | None = None) -> None:
    """Print aggregated API usage for the given month (YYYY-MM), defaulting to current month."""
    target = month or datetime.now().strftime("%Y-%m")

    gemini_calls = 0
    tokens_in = 0
    tokens_out = 0
    serper_calls = 0
    jina_calls = 0

    if not USAGE_FILE.exists():
        print(f"No usage data found (expected at {USAGE_FILE})")
        return

    with USAGE_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("ts", "").startswith(target):
                continue
            t = entry.get("type")
            if t == "gemini":
                gemini_calls += 1
                tokens_in += entry.get("tokens_in", 0)
                tokens_out += entry.get("tokens_out", 0)
            elif t == "serper":
                serper_calls += 1
            elif t == "jina":
                jina_calls += 1

    year, mo = target.split("-")
    month_name = datetime(int(year), int(mo), 1).strftime("%B %Y")
    print(f"API Usage — {month_name}")
    print("─" * 34)
    if gemini_calls:
        print(f"Gemini   {gemini_calls} calls   tokens in: {tokens_in:,}   tokens out: {tokens_out:,}")
    else:
        print("Gemini   0 calls")
    print(f"Serper   {serper_calls} searches")
    print(f"Jina     {jina_calls} fetches")
