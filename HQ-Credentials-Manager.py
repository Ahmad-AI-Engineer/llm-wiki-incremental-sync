from __future__ import annotations
import argparse
import getpass
import html
import http.cookiejar
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hq_common import (CRED_FILE, UI_DIR, HQError, add_common_arguments,
                       configure_logging, get_logger, now_iso, read_env)

LOGIN_HTML_FILE = UI_DIR / "login.html"
LOGIN_CSS_FILE = UI_DIR / "login.css"
SUCCESS_HTML_FILE = UI_DIR / "success.html"

SUCCESS_FALLBACK = ("<!doctype html><meta charset=\"utf-8\">"
                    "<title>Authenticated</title><p>Signed in. "
                    "You can close this tab.")

SHUTDOWN_GRACE = 0.8

DEFAULT_URL = "https://theaxiomworld.com"
UI_HOST, UI_PORT = "127.0.0.1", 8765
USER_AGENT = "HQ-Credentials-Manager/1.0"

log = get_logger("credentials-manager")


class OdooAuthError(HQError):
    pass


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def env_defaults() -> dict[str, str]:
    env = read_env()
    return {"url": normalize_url(env.get("ODOO_URL") or DEFAULT_URL),
            "username": env.get("ODOO_USERNAME") or ""}


def _ssl_context(verify: bool):
    return None if verify else ssl._create_unverified_context()


def jsonrpc(url: str, path: str, params: dict, session_id: str | None = None,
            verify_ssl: bool = True, timeout: int = 60):
    return jsonrpc_session(url, path, params, session_id, verify_ssl, timeout)[0]


def jsonrpc_session(url: str, path: str, params: dict,
                    session_id: str | None = None, verify_ssl: bool = True,
                    timeout: int = 60) -> tuple:
    payload = json.dumps({"jsonrpc": "2.0", "method": "call",
                          "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if session_id:
        headers["Cookie"] = f"session_id={session_id}"
        headers["X-Openerp-Session-Id"] = session_id
    request = urllib.request.Request(url + path, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=_ssl_context(verify_ssl)) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
            cookies = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raise OdooAuthError(f"HTTP {exc.code} from {path}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OdooAuthError(f"Cannot reach {url}: {exc.reason}") from exc

    if "error" in body:
        data = body["error"].get("data") or {}
        message = data.get("message") or body["error"].get("message") or "unknown error"
        raise OdooAuthError(f"{data.get('name', 'OdooError')}: {str(message).strip()}")
    rotated = next((found.group(1) for found in
                    (re.match(r"\s*session_id=([^;]+)", c) for c in cookies)
                    if found), None)
    return body.get("result"), rotated


class OdooSessionAuth:
    def __init__(self, url: str, verify_ssl: bool = True) -> None:
        self.url = normalize_url(url)
        self.verify_ssl = verify_ssl
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not verify_ssl:
            handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(False)))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def _cookie(self, name: str = "session_id") -> str | None:
        for cookie in self.jar:
            if cookie.name == name:
                return cookie.value
        return None

    def server_version(self) -> dict:
        return jsonrpc(self.url, "/web/webclient/version_info", {},
                       verify_ssl=self.verify_ssl) or {}

    def login(self, login: str, password: str, db: str = "") -> dict:
        if not login or not password:
            raise OdooAuthError("Username and password are both required.")

        session_id = self._form_login(login, password)
        if not session_id and db:
            session_id = self._jsonrpc_login(login, password, db)
        if not session_id:
            raise OdooAuthError("Login failed: Odoo did not return a session.")

        info = jsonrpc(self.url, "/web/session/get_session_info", {}, session_id,
                       verify_ssl=self.verify_ssl) or {}
        if not info.get("uid"):
            raise OdooAuthError("A session was created but it is not authenticated. "
                                "If this account uses two-factor authentication, "
                                "log in through Odoo in a browser first.")

        version = info.get("server_version") or self.server_version().get("server_version")
        company = info.get("user_companies") or {}
        return {
            "odoo_url": self.url,
            "session_id": session_id,
            "db": info.get("db"),
            "uid": info.get("uid"),
            "username": info.get("username") or login,
            "name": info.get("name"),
            "partner_id": info.get("partner_id"),
            "company_id": (info.get("user_context") or {}).get("allowed_company_ids",
                                                               [None])[0],
            "companies": company.get("allowed_companies") if isinstance(company, dict) else None,
            "user_context": info.get("user_context"),
            "server_version": version,
            "is_admin": info.get("is_admin"),
            "verify_ssl": self.verify_ssl,
            "created_at": now_iso(),
            "last_validated_at": now_iso(),
        }

    def _form_login(self, login: str, password: str) -> str | None:
        try:
            with self.opener.open(f"{self.url}/web/login", timeout=60) as response:
                page = response.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise OdooAuthError(f"Cannot reach {self.url}/web/login: {exc.reason}") from exc

        match = re.search(r'name="csrf_token"\s+value="([0-9a-zA-Z]+)"', page)
        if not match:
            return None

        form = {"login": login, "password": password,
                "csrf_token": match.group(1), "redirect": ""}
        request = urllib.request.Request(
            f"{self.url}/web/login", data=urllib.parse.urlencode(form).encode())
        try:
            with self.opener.open(request, timeout=60) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise OdooAuthError(f"Login request failed: {exc.reason}") from exc

        if 'name="csrf_token"' in body and "/web/login" in body:
            alert = re.search(r'alert alert-danger[^>]*>\s*([^<]{4,200})', body)
            reason = alert.group(1).strip() if alert else "Wrong login/password"
            if "totp" in body.lower() or "two-factor" in body.lower():
                raise OdooAuthError("This account requires two-factor authentication, "
                                    "which this login flow cannot complete.")
            raise OdooAuthError(f"Odoo rejected the credentials: {reason}")
        return self._cookie()

    def _jsonrpc_login(self, login: str, password: str, db: str) -> str | None:
        result = jsonrpc(self.url, "/web/session/authenticate",
                         {"db": db, "login": login, "password": password},
                         verify_ssl=self.verify_ssl)
        if not (result and result.get("uid")):
            return None
        return result.get("session_id") or self._cookie()


def save_credentials(record: dict, path: Path = CRED_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_credentials(path: Path = CRED_FILE) -> dict:
    if not path.exists():
        raise OdooAuthError(
            f"No stored session at {path}.\n"
            "Run:  python HQ-Credentials-Manager.py ui     (or 'login')")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_credentials(record: dict) -> dict:
    info, rotated = jsonrpc_session(record["odoo_url"],
                                    "/web/session/get_session_info", {},
                                    record.get("session_id"),
                                    record.get("verify_ssl", True))
    info = info or {}
    if not info.get("uid"):
        raise OdooAuthError("The stored session has expired. Log in again.")
    if rotated and rotated != record.get("session_id"):
        log.info("odoo rotated the session; storing the new one")
        record["session_id"] = rotated
        record["rotated_at"] = now_iso()
    record["uid"] = info["uid"]
    record["username"] = info.get("username", record.get("username"))
    record["db"] = info.get("db", record.get("db"))
    record["last_validated_at"] = now_iso()
    return record


def redact(record: dict) -> dict:
    out = dict(record)
    token = out.get("session_id") or ""
    out["session_id"] = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "***"
    return out


def load_login_template() -> str:
    try:
        return LOGIN_HTML_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise OdooAuthError(f"Cannot read the login page at "
                            f"{LOGIN_HTML_FILE}: {exc}") from None


def load_success_page() -> str:
    try:
        return SUCCESS_HTML_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cannot read %s (%s), using the plain success page",
                    SUCCESS_HTML_FILE.name, exc)
        return SUCCESS_FALLBACK


class UIHandler(BaseHTTPRequestHandler):
    server_version = "HQCredentialsManager/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render(self, message_html: str = "", status: int = 200) -> None:
        defaults = env_defaults()
        try:
            template = load_login_template()
        except OdooAuthError as exc:
            log.error("%s", exc)
            self.send_error(500, "Login page template is missing")
            return
        page = template.format(message=message_html,
                               url=html.escape(defaults["url"], quote=True),
                               username=html.escape(defaults["username"], quote=True))
        self._send(page.encode("utf-8"), "text/html; charset=utf-8", status)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._render()
        elif path == "/login.css":
            try:
                self._send(LOGIN_CSS_FILE.read_bytes(), "text/css; charset=utf-8")
            except OSError:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/login":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        def field(key: str) -> str:
            return (form.get(key) or [""])[0].strip()

        try:
            auth = OdooSessionAuth(env_defaults()["url"])
            record = auth.login(field("username"), field("password"))
            path = save_credentials(record)
            self._send(load_success_page().encode("utf-8"),
                       "text/html; charset=utf-8")
            log.info("authenticated %s (uid %s) db=%s -> %s",
                     record["name"], record["uid"], record["db"], path)
            self._signal_success(record)
        except OdooAuthError as exc:
            self._render(f'<div class="msg err">{html.escape(str(exc))}</div>')
            log.error("login failed: %s", exc)

    def _signal_success(self, record: dict) -> None:
        event = getattr(self.server, "login_event", None)
        if event is not None:
            self.server.login_record = record
            event.set()


def login_via_browser(host: str = UI_HOST, port: int = UI_PORT,
                      open_browser: bool = True) -> dict:
    load_login_template()
    server = ThreadingHTTPServer((host, port), UIHandler)
    server.login_event = threading.Event()
    server.login_record = None

    url = f"http://{host}:{port}/"
    print(f"\n  Odoo login required -> {url}")
    print("  Waiting for you to sign in (Ctrl+C to cancel)...")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:
            log.debug("could not open a browser: %s", exc)

    try:
        while not server.login_event.wait(0.5):
            pass
        time.sleep(SHUTDOWN_GRACE)
    except KeyboardInterrupt:
        raise OdooAuthError("Login cancelled.") from None
    finally:
        server.shutdown()
        server.server_close()

    return server.login_record


def ensure_session(open_browser: bool = True) -> dict:
    try:
        record = validate_credentials(load_credentials())
    except (OdooAuthError, json.JSONDecodeError, KeyError) as exc:
        log.info("no usable session (%s)", str(exc).splitlines()[0])
        record = login_via_browser(open_browser=open_browser)
    save_credentials(record)
    log.info("session valid: %s (uid %s) on db %s",
             record.get("username"), record.get("uid"), record.get("db"))
    return record


def cmd_ui(args: argparse.Namespace) -> int:
    load_login_template()
    server = ThreadingHTTPServer((args.host, args.port), UIHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"HQ Credentials Manager UI -> {url}")
    print("Enter your Odoo credentials in the browser. Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:
            log.debug("could not open a browser: %s", exc)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    defaults = env_defaults()
    url = args.url or defaults["url"]
    username = args.username or defaults["username"] or input("Odoo username: ").strip()
    password = args.password or os.environ.get("ODOO_PASSWORD") or getpass.getpass(
        f"Password for {username}: ")
    auth = OdooSessionAuth(url, verify_ssl=not args.insecure)
    record = auth.login(username, password, args.db or "")
    path = save_credentials(record)
    print(f"Authenticated as {record['name']} <{record['username']}> "
          f"(uid {record['uid']})")
    print(f"  database:       {record['db']}")
    print(f"  server version: {record['server_version']}")
    print(f"  token saved to: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    record = validate_credentials(load_credentials())
    save_credentials(record)
    print(json.dumps(redact(record), indent=2, default=str))
    print("\nSession is valid.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if not CRED_FILE.exists():
        print("No stored session.")
        return 0
    record = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    try:
        jsonrpc(record["odoo_url"], "/web/session/destroy", {},
                record.get("session_id"), record.get("verify_ssl", True))
    except OdooAuthError as exc:
        log.debug("server-side session destroy failed (already expired?): %s", exc)
    CRED_FILE.unlink()
    log.info("removed %s", CRED_FILE)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="HQ-Credentials-Manager.py",
        description="Authenticate against Odoo and store a session token.")
    add_common_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    ui = sub.add_parser("ui", help="serve the browser login UI")
    ui.add_argument("--host", default=UI_HOST)
    ui.add_argument("--port", type=int, default=UI_PORT)
    ui.add_argument("--no-browser", action="store_true")
    ui.set_defaults(func=cmd_ui)

    login = sub.add_parser("login", help="authenticate from the terminal")
    login.add_argument("--url", default=None)
    login.add_argument("--username", default=None)
    login.add_argument("--password", default=None,
                       help="omit to be prompted without echo")
    login.add_argument("--db", default=None,
                       help="only needed if form login is unavailable")
    login.add_argument("--insecure", action="store_true", help="skip TLS verification")
    login.set_defaults(func=cmd_login)

    sub.add_parser("status", help="validate and show the stored session").set_defaults(
        func=cmd_status)
    sub.add_parser("logout", help="destroy and delete the stored session").set_defaults(
        func=cmd_logout)

    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        return args.func(args)
    except HQError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
