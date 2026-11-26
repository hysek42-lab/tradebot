import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_config(path: Path | None = None) -> dict:
    load_env(ROOT / ".env")
    cfg = yaml.safe_load((path or ROOT / "config.yaml").read_text())
    env = cfg["environment"]
    if env not in ("demo", "live"):
        raise ValueError("environment must be 'demo' or 'live'")
    prefix = "T212_DEMO" if env == "demo" else "T212_LIVE"
    cfg["api_key"] = os.environ.get(f"{prefix}_API_KEY", "")
    cfg["api_secret"] = os.environ.get(f"{prefix}_API_SECRET", "")
    cfg["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", "")
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")
    cfg["data_dir"] = Path(os.environ.get("BOT_DATA_DIR", ROOT / "data"))
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    a = cfg["allocation"]
    if a["core_pct"] + a["active_pct"] > 1.0001:
        raise ValueError("core_pct + active_pct must be <= 1")
    return cfg
