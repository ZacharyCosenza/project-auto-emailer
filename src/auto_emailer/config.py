import json
import os


_REQUIRED_STRUCTURE = {
    "email": ["smtp_host", "smtp_port", "sender", "recipients"],
    "llm": ["model"],
    "schedule": ["cron"],
}


def validate_config(config: dict) -> None:
    missing = []
    for section, keys in _REQUIRED_STRUCTURE.items():
        if section not in config:
            missing.append(section)
        else:
            for key in keys:
                if key not in config[section]:
                    missing.append(f"{section}.{key}")
    if "searches" not in config and "events" not in config:
        missing.append("searches")
    if missing:
        raise ValueError(f"Missing required config keys: {', '.join(missing)}")


def load_config(path: str = "config.json", *, require_secrets: bool = True) -> dict:
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file: {path}: {e}")

    validate_config(config)

    password = os.environ.get("AUTO_EMAILER_SMTP_PASSWORD", "")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if require_secrets:
        missing = []
        if not password:
            missing.append("AUTO_EMAILER_SMTP_PASSWORD")
        if not api_key:
            missing.append("GEMINI_API_KEY")
        if missing:
            raise EnvironmentError(
                f"Required environment variables not set: {', '.join(missing)}"
            )
    config["email"]["password"] = password
    config["llm"]["api_key"] = api_key
    return config
