"""Install auto-emailer as a systemd user timer.

Writes ~/.config/systemd/user/auto-emailer.{service,timer} and an env file,
then enables and starts the timer via systemctl --user.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

_DOW_MAP = {
    "0": "Sun", "7": "Sun",
    "1": "Mon", "2": "Tue", "3": "Wed",
    "4": "Thu", "5": "Fri", "6": "Sat",
}

_ENV_KEYS = [
    "GEMINI_API_KEY",
    "SERPER_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "AUTO_EMAILER_SMTP_PASSWORD",
]


def _cron_to_on_calendar(cron: str) -> str:
    """Convert a simple 5-part cron string to a systemd OnCalendar value.

    Supports:
      'M H * * DOW' (weekly)   e.g. '0 8 * * 5'  → 'Fri *-*-* 08:00:00'
      'M H * * *'  (daily)     e.g. '0 0 * * *'  → '*-*-* 00:00:00'
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-part cron string, got: {cron!r}")
    minute, hour, day, month, dow = parts
    if day != "*" or month != "*":
        raise ValueError(
            f"Only daily/weekly cron patterns ('M H * * *' or 'M H * * DOW') are supported. Got: {cron!r}\n"
            "Edit ~/.config/systemd/user/auto-emailer.timer manually for other patterns."
        )
    hh = hour.zfill(2)
    mm = minute.zfill(2)
    if dow == "*":
        return f"*-*-* {hh}:{mm}:00"
    if dow not in _DOW_MAP:
        raise ValueError(f"Unrecognised day-of-week value {dow!r} in cron: {cron!r}")
    return f"{_DOW_MAP[dow]} *-*-* {hh}:{mm}:00"


def install(config: dict, config_path: str) -> None:
    """Install systemd user timer + service, write env file, enable and start."""
    cron = (config.get("schedule") or {}).get("cron", "0 8 * * 5")
    on_calendar = _cron_to_on_calendar(cron)

    python = sys.executable
    config_abs = str(Path(config_path).resolve())
    work_dir = str(Path(config_abs).parent)

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    env_dir = Path.home() / ".config" / "auto-emailer"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / "env"

    # --- Write env file (only keys present in the current environment) ---
    env_lines = [f"{k}={os.environ[k]}" for k in _ENV_KEYS if k in os.environ]
    env_file.write_text("\n".join(env_lines) + "\n")
    env_file.chmod(0o600)
    print(f"Wrote env file: {env_file}")

    # --- Write .service ---
    service_file = systemd_dir / "auto-emailer.service"
    service_file.write_text(f"""\
[Unit]
Description=NYC Weekend Events Auto-Emailer

[Service]
Type=oneshot
WorkingDirectory={work_dir}
EnvironmentFile=%h/.config/auto-emailer/env
ExecStart={python} -m auto_emailer --config {config_abs} run
StandardOutput=journal
StandardError=journal
""")
    print(f"Wrote service: {service_file}")

    # --- Write .timer ---
    timer_file = systemd_dir / "auto-emailer.timer"
    timer_file.write_text(f"""\
[Unit]
Description=NYC Weekend Events Auto-Emailer Timer

[Timer]
OnCalendar={on_calendar}
Persistent=true
Unit=auto-emailer.service

[Install]
WantedBy=timers.target
""")
    print(f"Wrote timer:   {timer_file}  (OnCalendar={on_calendar})")

    # --- Enable and start ---
    if not shutil.which("systemctl"):
        print("\nsystemctl not found — skipping enable/start.")
        print("To activate manually:\n  systemctl --user daemon-reload")
        print("  systemctl --user enable --now auto-emailer.timer")
        return

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "auto-emailer.timer"], check=True)

    print("\nInstalled successfully.")
    print(f"  Schedule:   {on_calendar}  (from cron: {cron})")
    print(f"  Status:     systemctl --user status auto-emailer.timer")
    print(f"  Logs:       journalctl --user -u auto-emailer -n 50")
    print(f"  Next run:   systemctl --user list-timers auto-emailer.timer")
