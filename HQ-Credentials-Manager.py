from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hq_common import (BASE_DIR, add_common_arguments, now_iso, read_env,
                       setup_logging)

CRED_DIR = BASE_DIR / "credentials"
CRED_FILE = CRED_DIR / "odoo_session.json"

DEFAULT_URL = "https://theaxiomworld.com"
UI_HOST, UI_PORT = "127.0.0.1", 8765
USER_AGENT = "HQ-Credentials-Manager/1.0"

log = setup_logging("credentials-manager")


class OdooAuthError(RuntimeError):
    pass


def _now() -> str:
    return now_iso()


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
    payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if session_id:
        headers["Cookie"] = f"session_id={session_id}"
        headers["X-Openerp-Session-Id"] = session_id
    request = urllib.request.Request(url + path, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=_ssl_context(verify_ssl)) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise OdooAuthError(f"HTTP {exc.code} from {path}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OdooAuthError(f"Cannot reach {url}: {exc.reason}") from exc

    if "error" in body:
        data = body["error"].get("data") or {}
        message = data.get("message") or body["error"].get("message") or "unknown error"
        raise OdooAuthError(f"{data.get('name', 'OdooError')}: {str(message).strip()}")
    return body.get("result")


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
            "created_at": _now(),
            "last_validated_at": _now(),
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
    info = jsonrpc(record["odoo_url"], "/web/session/get_session_info", {},
                   record.get("session_id"), record.get("verify_ssl", True)) or {}
    if not info.get("uid"):
        raise OdooAuthError("The stored session has expired. Log in again.")
    record["uid"] = info["uid"]
    record["username"] = info.get("username", record.get("username"))
    record["db"] = info.get("db", record.get("db"))
    record["last_validated_at"] = _now()
    return record


def redact(record: dict) -> dict:
    out = dict(record)
    token = out.get("session_id") or ""
    out["session_id"] = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "***"
    return out


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>HQ Credentials Manager</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 26rem; margin: 4rem auto;
         padding: 0 1.25rem; }}
  h1 {{ font-size: 1.15rem; margin-bottom: .25rem; }}
  p.sub {{ color: #6b7280; margin-top: 0; font-size: .9rem; }}
  label {{ display: block; margin: .9rem 0 .25rem; font-weight: 600; font-size: .82rem;
          text-transform: uppercase; letter-spacing: .04em; color: #6b7280; }}
  input[type=text], input[type=password] {{ width: 100%; padding: .55rem .65rem;
          border: 1px solid #9ca3af; border-radius: 6px; background: transparent;
          color: inherit; font: inherit; }}
  button {{ margin-top: 1.4rem; width: 100%; padding: .6rem; border: 0; border-radius: 6px;
           background: #4f46e5; color: #fff; font: inherit; font-weight: 600;
           cursor: pointer; }}
  .msg {{ margin-top: 1.2rem; padding: .7rem .8rem; border-radius: 6px; font-size: .9rem;
         white-space: pre-wrap; }}
  .ok {{ background: #dcfce7; color: #14532d; }}
  .err {{ background: #fee2e2; color: #7f1d1d; }}
  code {{ font-size: .85em; }}
</style></head><body>
<h1>HQ Credentials Manager</h1>
<p class="sub">Sign in to Odoo. The password is used once to mint a session token
and is never stored.</p>
{message}
<form method="post" action="/login">
  <label for="url">Odoo URL</label>
  <input type="text" id="url" name="url" value="{url}" required>
  <label for="username">Username</label>
  <input type="text" id="username" name="username" value="{username}"
         autocomplete="username" required>
  <label for="password">Password</label>
  <input type="password" id="password" name="password" autocomplete="current-password"
         required autofocus>
  <button type="submit">Authenticate</button>
</form>
</body></html>
"""


class UIHandler(BaseHTTPRequestHandler):
    server_version = "HQCredentialsManager/1.0"

    def log_message(self, fmt, *args):
        pass

    def _render(self, message_html: str = "", status: int = 200) -> None:
        defaults = env_defaults()
        page = LOGIN_PAGE.format(message=message_html, url=defaults["url"],
                                 username=defaults["username"])
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/":
            self.send_error(404)
            return
        self._render()

    def do_POST(self) -> None:
        if self.path != "/login":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        def field(key: str) -> str:
            return (form.get(key) or [""])[0].strip()

        try:
            auth = OdooSessionAuth(field("url") or DEFAULT_URL,
                                   verify_ssl=field("verify_ssl") != "false")
            record = auth.login(field("username"), field("password"), field("db"))
            path = save_credentials(record)
            self._render(
                f'<div class="msg ok">Authenticated as <strong>{record["name"]}'
                f'</strong> (uid {record["uid"]}) on database <code>{record["db"]}</code>, '
                f'Odoo {record["server_version"]}.\n\nToken saved to '
                f'<code>{path.relative_to(BASE_DIR)}</code>. You can close this tab '
                f'and run HQ-Scanner.py.</div>')
            log.info("authenticated %s (uid %s) db=%s -> %s",
                     record["name"], record["uid"], record["db"], path)
        except OdooAuthError as exc:
            self._render(f'<div class="msg err">{exc}</div>', status=200)
            log.error("login failed: %s", exc)


def cmd_ui(args: argparse.Namespace) -> int:
    server = ThreadingHTTPServer((args.host, args.port), UIHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"HQ Credentials Manager UI -> {url}")
    print("Enter your Odoo credentials in the browser. Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # headless environments simply skip this
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
    print(f"Authenticated as {record['name']} <{record['username']}> (uid {record['uid']})")
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
    login.add_argument("--db", default=None, help="only needed if form login is unavailable")
    login.add_argument("--insecure", action="store_true", help="skip TLS verification")
    login.set_defaults(func=cmd_login)

    sub.add_parser("status", help="validate and show the stored session").set_defaults(
        func=cmd_status)
    sub.add_parser("logout", help="destroy and delete the stored session").set_defaults(
        func=cmd_logout)

    args = parser.parse_args(argv)
    if args.verbose:
        setup_logging("credentials-manager", verbose=True)
    try:
        return args.func(args)
    except OdooAuthError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
