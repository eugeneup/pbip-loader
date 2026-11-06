"""Settings shared by the terminal interface and the CLI."""
import json

from .auth import config_directory
from .storage import dump


def settings_path():
    return config_directory() / "settings.json"


def load_settings(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("root"), str):
            return {"root": value["root"]}
    except (OSError, ValueError):
        pass
    return {}


def save_settings(path, *, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"root": root}
    dump(path, settings)
    return settings
