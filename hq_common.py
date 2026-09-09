from __future__ import annotations
import logging
import logging.handlers
import os
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
OUT_DIR = BASE_DIR / "output"
HISTORY_DIR = OUT_DIR / "history"
LOG_DIR = OUT_DIR / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def read_env() -> dict[str, str]:
    """Real environment variables win over .env entries."""
    values = dict(os.environ)
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def env_positive_int(name: str, env: dict[str, str] | None = None) -> int | None:
    """Read an optional positive-integer setting. Empty or absent returns None.

    Raises ValueError for a non-numeric or non-positive value, so a typo in
    .env fails loudly instead of silently disabling the setting.
    """
    env = env if env is not None else read_env()
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} in .env is not a number: {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} in .env must be 1 or more (got {value}); "
                         "leave it empty for no limit")
    return value


def next_run_id(prefix: str, history_dir: Path = HISTORY_DIR) -> str:
    """Sequential id per source per day, e.g. 20260909-003."""
    day = today_stamp()
    used = []
    if history_dir.exists():
        for path in history_dir.glob(f"{prefix}_{day}-*.json"):
            found = re.search(rf"{re.escape(prefix)}_{day}-(\d+)$", path.stem)
            if found:
                used.append(int(found.group(1)))
    return f"{day}-{max(used, default=0) + 1:03d}"


def setup_logging(name: str, verbose: bool = False) -> logging.Logger:
    """Console logging plus a shared rotating file at output/logs/pipeline.log."""
    env = read_env()
    level_name = (env.get("LOG_LEVEL") or ("DEBUG" if verbose else "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not any(getattr(h, "_hq_pipeline", False) for h in root.handlers):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        console._hq_pipeline = True
        root.addHandler(console)

        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
            file_handler._hq_pipeline = True
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("file logging disabled: %s", exc)

    return logging.getLogger(name)


def add_common_arguments(parser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug-level logging")
