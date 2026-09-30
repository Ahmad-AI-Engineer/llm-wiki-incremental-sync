from __future__ import annotations
import html
import http.client
import json
import logging
import logging.handlers
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
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

TASK_DESCRIPTION_BATCH = 100

TITLE_LIMIT = 100

TASK_SECTION_TITLES = {"tasks", "task", "task list", "tasks detail",
                       "operational tasks"}

RATE_LIMIT_RETRIES = 12
RATE_LIMIT_FALLBACK = 30.0
PACING_START = 2.0
PACING_STEP = 0.4
PACING_MAX = 3.0
SERVER_ERROR_CODES = {500, 502, 503, 504}
CREATE_ATTEMPTS = 4
CREATE_LOOKBACK = 50
CLOCK_SKEW_SECONDS = 120

log = logging.getLogger(__name__)


class HQError(RuntimeError):
    pass


class OutlineError(HQError):
    pass


class OutlineUncertainError(OutlineError):
    pass


class OdooError(HQError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def today_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def read_env() -> dict[str, str]:
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
    return logging.getLogger(name)


def configure_logging(verbose: bool = False) -> None:
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
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def add_common_arguments(parser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug-level logging")


IMAGE_PLACEHOLDER = "_[screenshot — see the task in Odoo]_"

INLINE_MARKS = {"b": "**", "strong": "**", "i": "*", "em": "*"}
BLOCK_TAGS = {"p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4",
              "h5", "h6", "blockquote"}
SKIP_TAGS = {"script", "style", "head"}

INDENT = ""


def clean_title(name: str, limit: int = TITLE_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def render_table(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    escaped = [[cell.replace("|", r"\|") for cell in row] for row in padded]
    header, *body = escaped
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n\n" + "\n".join(lines) + "\n\n"


class _MarkdownWriter(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[str] = []
        self.item_index: list[int] = []
        self.tables: list[list[list[str]]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.link: str | None = None
        self.skip = 0
        self.li_depth = 0

    def emit(self, text: str) -> None:
        if self.cell is not None:
            self.cell.append(text)
        else:
            self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "img":
            self.emit(" " + IMAGE_PLACEHOLDER + " " if self.cell is not None
                      else "\n\n" + IMAGE_PLACEHOLDER + "\n\n")
        elif tag == "br":
            self.emit(" " if self.cell is not None else "\n")
        elif tag in INLINE_MARKS:
            self.emit(INLINE_MARKS[tag])
        elif tag == "a":
            self.link = attrs.get("href")
            self.emit("[")
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr":
            if self.tables:
                self.row = []
        elif tag in ("td", "th"):
            if self.row is not None:
                self.cell = []
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag)
            self.item_index.append(0)
            if not self.li_depth:
                self.emit("\n")
        elif tag == "li":
            self.li_depth += 1
            depth = INDENT * max(len(self.list_stack) - 1, 0)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.item_index[-1] += 1
                self.emit(f"\n{depth}{self.item_index[-1]}. ")
            else:
                self.emit(f"\n{depth}- ")
        elif tag in BLOCK_TAGS:
            self.emit(" " if self.li_depth else "\n\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(self.skip - 1, 0)
        elif tag in INLINE_MARKS:
            self.emit(INLINE_MARKS[tag])
        elif tag == "a":
            target = self.link or ""
            self.emit(f"]({target})" if target else "]")
            self.link = None
        elif tag in ("td", "th"):
            if self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr":
            if self.row is not None and self.tables:
                self.tables[-1].append(self.row)
            self.row = None
        elif tag == "table":
            if self.tables:
                self.parts.append(render_table(self.tables.pop()))
        elif tag == "li":
            self.li_depth = max(self.li_depth - 1, 0)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.item_index.pop()
            self.emit("\n")
        elif tag in BLOCK_TAGS:
            self.emit(" " if self.li_depth else "\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.emit(data.replace("\xa0", " "))

    def text(self) -> str:
        body = "".join(self.parts)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r" *\n *", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body.replace(INDENT, "  ").strip()


def html_to_markdown(source: str) -> str:
    if not source:
        return ""
    if "<" not in source:
        return html.unescape(source).strip()
    writer = _MarkdownWriter()
    try:
        writer.feed(source)
        writer.close()
    except Exception as exc:
        log.warning("could not parse a description as HTML (%s), "
                    "falling back to stripped text", exc)
        return html.unescape(re.sub(r"<[^>]+>", " ", source)).strip()
    return writer.text()


def strip_images(source: str) -> str:
    return re.sub(r"<img\b[^>]*>", "<img>", source or "", flags=re.IGNORECASE)


def outline_credentials() -> tuple[str, str, str]:
    env = read_env()
    key = (env.get("OUTLINE_API_KEY") or env.get("Outline API Key")
           or env.get("OUTLINE_TOKEN") or "").strip()
    if not key:
        blob = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        found = re.search(r"(ol_api_[A-Za-z0-9]+)", blob)
        key = found.group(1) if found else ""
    if not key:
        raise OutlineError("No Atlas API key found. Add OUTLINE_API_KEY=... to .env")
    url = (env.get("OUTLINE_URL") or DEFAULT_OUTLINE_URL).strip().rstrip("/")
    collection = (env.get("OUTLINE_COLLECTION_ID") or DEFAULT_COLLECTION_ID).strip()
    return url, key, collection


class OutlineClient:

    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key
        self.min_interval = 0.0
        self._last_call = 0.0
        self._paced = False
        self._settled = False

    def _pace(self) -> None:
        if self.min_interval:
            gap = time.monotonic() - self._last_call
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def _slow_down(self, endpoint: str) -> None:
        self.min_interval = min(max(self.min_interval + PACING_STEP, PACING_START),
                                PACING_MAX)
        self._settled = False
        if not self._paced:
            self._paced = True
            log.warning("atlas rate limited on %s; backing off", endpoint)
        else:
            log.debug("atlas still rate limited on %s; interval now %.1fs",
                      endpoint, self.min_interval)

    def _note_settled(self) -> None:
        if self._paced and not self._settled:
            self._settled = True
            log.info("atlas creates settled at about %.0f/min",
                     60 / self.min_interval)

    def call(self, endpoint: str, payload: dict | None = None,
             timeout: int = 90, attempts: int = 4,
             idempotent: bool = True) -> dict:
        body = json.dumps(payload or {}).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        network_tries = 0
        limit_tries = 0
        transient_tries = 0

        def transient(problem: str, cause: BaseException) -> None:
            nonlocal transient_tries
            if not idempotent:
                raise OutlineUncertainError(
                    f"{problem} on {endpoint}; the write may or may not have "
                    "landed") from cause
            transient_tries += 1
            if transient_tries >= attempts:
                raise OutlineError(f"{problem} on {endpoint}, gave up after "
                                   f"{attempts} tries") from cause
            log.warning("%s on %s, retrying (%d/%d)", problem, endpoint,
                        transient_tries, attempts - 1)
            time.sleep(2 ** transient_tries)

        while True:
            self._pace()
            request = urllib.request.Request(f"{self.url}/api/{endpoint}",
                                             data=body, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8", "replace"))
                self._note_settled()
                return result
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and limit_tries < RATE_LIMIT_RETRIES:
                    limit_tries += 1
                    wait = float(exc.headers.get("Retry-After")
                                 or RATE_LIMIT_FALLBACK)
                    self._slow_down(endpoint)
                    log.debug("rate limited on %s, waiting %.0fs (%d)",
                              endpoint, wait, limit_tries)
                    time.sleep(wait)
                    continue
                if exc.code in SERVER_ERROR_CODES:
                    transient(f"Atlas HTTP {exc.code}", exc)
                    continue
                detail = exc.read()[:400].decode("utf-8", "replace")
                raise OutlineError(
                    f"Atlas HTTP {exc.code} on {endpoint}: {detail}") from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    transient("Atlas timed out", exc)
                    continue
                network_tries += 1
                if network_tries < attempts:
                    log.warning("%s unreachable (%s), retrying", endpoint, exc.reason)
                    time.sleep(2 ** network_tries)
                    continue
                raise OutlineError(f"Cannot reach {self.url}: {exc.reason}") from exc
            except TimeoutError as exc:
                transient("Atlas timed out", exc)
                continue
            except (ConnectionError, http.client.HTTPException) as exc:
                transient(f"Atlas connection dropped ({type(exc).__name__})", exc)
                continue
            except json.JSONDecodeError as exc:
                transient("Atlas sent an unreadable response", exc)
                continue

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
        started = time.time()
        for attempt in range(1, CREATE_ATTEMPTS + 1):
            try:
                created = self.call("documents.create", payload,
                                    idempotent=False).get("data") or {}
            except OutlineUncertainError as exc:
                if attempt == CREATE_ATTEMPTS:
                    raise OutlineError(f"Could not confirm {title!r} was created "
                                       f"after {attempt} tries: {exc}") from exc
                time.sleep(2 ** attempt)
                landed = self.find_created(title, collection_id, parent_id,
                                           started)
                if landed:
                    log.warning("create of %r got no clear answer, but it "
                                "landed; using %s", title, landed["id"])
                    return landed
                log.warning("create of %r got no clear answer and did not land; "
                            "retrying (%d/%d)", title, attempt,
                            CREATE_ATTEMPTS - 1)
                continue
            if not created.get("id"):
                raise OutlineError(f"Atlas returned no document id for {title!r}")
            return created
        raise OutlineError(f"Could not create {title!r}")

    def find_created(self, title: str, collection_id: str,
                     parent_id: str | None, since: float) -> dict | None:
        payload: dict[str, Any] = {"collectionId": collection_id,
                                   "sort": "createdAt", "direction": "DESC",
                                   "limit": CREATE_LOOKBACK}
        if parent_id:
            payload["parentDocumentId"] = parent_id
        rows = self.call("documents.list", payload).get("data") or []
        cutoff = since - CLOCK_SKEW_SECONDS
        for row in rows:
            if (str(row.get("title") or "").strip() == title.strip()
                    and (row.get("parentDocumentId") or None) == (parent_id or None)
                    and _epoch(row.get("createdAt")) >= cutoff):
                return row
        return None

    def update_document(self, document_id: str, text: str | None = None,
                        title: str | None = None) -> dict:
        payload: dict[str, Any] = {"id": document_id}
        if text is not None:
            payload["text"] = text
        if title is not None:
            payload["title"] = title
        return self.call("documents.update", payload).get("data") or {}

    def document_children(self, document_id: str,
                          collection_id: str) -> list[dict]:
        def walk(nodes):
            for node in nodes:
                if node.get("id") == document_id:
                    return node.get("children") or []
                found = walk(node.get("children") or [])
                if found is not None:
                    return found
            return None
        return walk(self.collection_tree(collection_id)) or []


class OdooSessionClient:

    def __init__(self, credentials: dict) -> None:
        self.url = credentials["odoo_url"].rstrip("/")
        self.session_id = credentials["session_id"]
        self.db = credentials.get("db")
        self.uid = credentials.get("uid")
        self.username = credentials.get("username")
        self.name = credentials.get("name")
        self.server_version = credentials.get("server_version")
        self.verify_ssl = credentials.get("verify_ssl", True)

    @classmethod
    def from_file(cls, path: Path = CRED_FILE) -> "OdooSessionClient":
        if not path.exists():
            raise OdooError(
                f"No stored session at {path}\n"
                "Authenticate first:  python HQ-Credentials-Manager.py ui")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def call_kw(self, model: str, method: str, args: list,
                kwargs: dict | None = None, timeout: int = 180) -> Any:
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args, "kwargs": kwargs or {},
        }}).encode()
        request = urllib.request.Request(
            self.url + "/web/dataset/call_kw", data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "HQ-Pipeline/1.0",
                "Cookie": f"session_id={self.session_id}",
                "X-Openerp-Session-Id": self.session_id,
            })
        context = None if self.verify_ssl else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=context) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise OdooError(f"HTTP {exc.code} calling {model}.{method}: "
                            f"{exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise OdooError(f"Cannot reach {self.url}: {exc.reason}") from exc

        if "error" in body:
            data = body["error"].get("data") or {}
            name = data.get("name", "")
            message = str(data.get("message")
                          or body["error"].get("message") or "").strip()
            if "SessionExpired" in name or "Session expired" in message:
                raise OdooError("The stored session has expired. Re-authenticate:\n"
                                "  python HQ-Credentials-Manager.py ui")
            raise OdooError(f"{name or 'OdooError'} on {model}.{method}: {message}")
        return body.get("result")

    def search_read(self, model: str, domain: list, fields: list[str] | None = None,
                    order: str = "id asc", batch: int = 200,
                    limit: int | None = None) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            take = batch if limit is None else min(batch, limit - len(rows))
            if take <= 0:
                break
            kwargs: dict[str, Any] = {"offset": offset, "limit": take, "order": order}
            if fields:
                kwargs["fields"] = fields
            page = self.call_kw(model, "search_read", [domain], kwargs)
            rows.extend(page)
            if len(page) < take:
                break
            offset += take
        log.debug("read %d %s", len(rows), model)
        return rows


def load_task_descriptions(client: OdooSessionClient,
                           task_ids: list[int]) -> dict[int, str]:
    descriptions: dict[int, str] = {}
    for start in range(0, len(task_ids), TASK_DESCRIPTION_BATCH):
        chunk = task_ids[start:start + TASK_DESCRIPTION_BATCH]
        rows = client.search_read("project.task", [["id", "in", chunk]],
                                  fields=["id", "description"],
                                  batch=len(chunk))
        for row in rows:
            raw = strip_images(row.get("description") or "")
            descriptions[row["id"]] = html_to_markdown(raw)
    filled = sum(1 for text in descriptions.values() if text)
    log.debug("descriptions: %d of %d tasks have content",
              filled, len(descriptions))
    return descriptions
