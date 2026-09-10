"""Shared helpers for the HQ Odoo -> Outline pipeline.

The top-level scripts have hyphens in their names, which are not valid Python
module names, so they cannot import each other. Anything used by more than one
of them lives here: paths, the error base class, .env loading, run ids,
logging, and the Outline API client.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
UI_DIR = BASE_DIR / "ui"

CRED_FILE = BASE_DIR / "credentials" / "odoo_session.json"

OUT_DIR = BASE_DIR / "output"
HISTORY_DIR = OUT_DIR / "history"
LOG_DIR = OUT_DIR / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"

ODOO_SCAN_FILE = OUT_DIR / "projects_and_tasks.json"
OUTLINE_SCAN_FILE = OUT_DIR / "outline_projects.json"
MAPPING_FILE = OUT_DIR / "outline_mapping.json"
SYNC_LOG_FILE = OUT_DIR / "sync_log.md"

DEFAULT_OUTLINE_URL = "https://atlas.axm.app"
DEFAULT_COLLECTION_ID = "393ead77-88e3-46f3-8e16-dff19fafe1d1"

log = logging.getLogger(__name__)


class HQError(RuntimeError):
    """A failure with a message meant for the user.

    Every script subclasses this and reports it as a single error line instead
    of a traceback.
    """


class OutlineError(HQError):
    """The Outline API refused a call or could not be reached."""


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


CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def get_logger(name: str) -> logging.Logger:
    """A logger for one script. configure_logging installs the handlers."""
    return logging.getLogger(name)


def configure_logging(verbose: bool = False) -> None:
    """Install console and rotating-file logging. Safe to call more than once.

    Console level: --verbose wins, then LOG_LEVEL in .env, then INFO. The file
    at output/logs/pipeline.log always keeps a full DEBUG trail.

    Reports go to stdout and logs go to stderr, so `command > report.txt`
    keeps them apart.
    """
    _use_utf8_stdio()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = _handler(root, "console")
    if console is None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        console._hq_role = "console"
        root.addHandler(console)
    console.setLevel(logging.DEBUG if verbose else _console_level())

    if _handler(root, "file") is None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            to_file = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                encoding="utf-8")
            to_file.setLevel(logging.DEBUG)
            to_file.setFormatter(logging.Formatter(FILE_FORMAT))
            to_file._hq_role = "file"
            root.addHandler(to_file)
        except OSError as exc:
            root.warning("file logging disabled: %s", exc)


def _handler(root: logging.Logger, role: str) -> logging.Handler | None:
    return next((h for h in root.handlers
                 if getattr(h, "_hq_role", None) == role), None)


def _console_level() -> int:
    name = (read_env().get("LOG_LEVEL") or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _use_utf8_stdio() -> None:
    """Print UTF-8 whatever the console claims to be.

    Windows consoles default to cp1252, and some Odoo project names contain
    characters it cannot encode (a zero-width space, for one), which used to
    abort a report part-way through with UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def add_common_arguments(parser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug-level logging")


def outline_credentials() -> tuple[str, str, str]:
    """(url, api_key, collection_id) for the Outline knowledge base."""
    env = read_env()
    key = (env.get("OUTLINE_API_KEY") or env.get("Outline API Key")
           or env.get("OUTLINE_TOKEN") or "").strip()
    if not key:
        blob = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        found = re.search(r"(ol_api_[A-Za-z0-9]+)", blob)
        key = found.group(1) if found else ""
    if not key:
        raise OutlineError("No Outline API key found. Add OUTLINE_API_KEY=... to .env")
    url = (env.get("OUTLINE_URL") or DEFAULT_OUTLINE_URL).strip().rstrip("/")
    collection = (env.get("OUTLINE_COLLECTION_ID") or DEFAULT_COLLECTION_ID).strip()
    return url, key, collection


class OutlineClient:
    """Outline API client. Retries rate limits and transient network errors."""

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key

    def call(self, endpoint: str, payload: dict | None = None,
             timeout: int = 90, attempts: int = 4) -> dict:
        body = json.dumps(payload or {}).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(f"{self.url}/api/{endpoint}",
                                             data=body, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < attempts:
                    wait = float(exc.headers.get("Retry-After") or 2 ** attempt)
                    log.warning("rate limited on %s, waiting %.0fs", endpoint, wait)
                    time.sleep(wait)
                    continue
                detail = exc.read()[:400].decode("utf-8", "replace")
                raise OutlineError(
                    f"Outline HTTP {exc.code} on {endpoint}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < attempts:
                    log.warning("%s unreachable (%s), retrying", endpoint, exc.reason)
                    time.sleep(2 ** attempt)
                    continue
                raise OutlineError(f"Cannot reach {self.url}: {exc.reason}") from exc
        raise OutlineError(f"{endpoint} failed after {attempts} attempts")

    def whoami(self) -> dict:
        return self.call("auth.info").get("data") or {}

    def collection_info(self, collection_id: str) -> dict:
        return self.call("collections.info", {"id": collection_id}).get("data") or {}

    def collection_tree(self, collection_id: str) -> list[dict]:
        return self.call("collections.documents",
                         {"id": collection_id}).get("data") or []

    def all_documents(self, collection_id: str, batch: int = 100) -> list[dict]:
        documents: list[dict] = []
        offset = 0
        while True:
            page = self.call("documents.list", {"collectionId": collection_id,
                                                "limit": batch, "offset": offset})
            rows = page.get("data") or []
            documents.extend(rows)
            if len(rows) < batch:
                break
            offset += batch
        log.debug("fetched %d documents in %d page(s)",
                  len(documents), offset // batch + 1)
        return documents

    def create_document(self, title: str, text: str, collection_id: str,
                        parent_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"title": title, "text": text,
                                   "collectionId": collection_id, "publish": True}
        if parent_id:
            payload["parentDocumentId"] = parent_id
        created = self.call("documents.create", payload).get("data") or {}
        if not created.get("id"):
            raise OutlineError(f"Outline returned no document id for {title!r}")
        return created
