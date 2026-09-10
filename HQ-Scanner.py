"""Scan Odoo projects and tasks by practice, using a stored session."""
from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hq_common import (CRED_FILE, HISTORY_DIR, ODOO_SCAN_FILE, OUT_DIR, HQError,
                       add_common_arguments, configure_logging, get_logger,
                       next_run_id, now_iso)

USER_AGENT = "HQ-Scanner/1.0"

PROJECT_SCAN_FIELDS = ["id", "name", "active", "sale_line_id"]
TASK_SCAN_FIELDS = ["id", "name", "project_id", "user_ids"]

GENERIC_CATEGORIES = {"all", "all / saleable", "all / expenses", "all / deliveries",
                      "all / saleable / events", "goods", "services"}

log = get_logger("odoo-scanner")


class ScanError(HQError):
    pass


def name_of(value: Any) -> str | None:
    """Odoo returns a many-to-one as [id, display_name]."""
    return value[1] if isinstance(value, list) and len(value) > 1 else None


def id_of(value: Any) -> int | None:
    return value[0] if isinstance(value, list) and value else None


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
            raise ScanError(
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
                "User-Agent": USER_AGENT,
                "Cookie": f"session_id={self.session_id}",
                "X-Openerp-Session-Id": self.session_id,
            })
        context = None if self.verify_ssl else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=context) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ScanError(f"HTTP {exc.code} calling {model}.{method}: "
                            f"{exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ScanError(f"Cannot reach {self.url}: {exc.reason}") from exc

        if "error" in body:
            data = body["error"].get("data") or {}
            name = data.get("name", "")
            message = str(data.get("message")
                          or body["error"].get("message") or "").strip()
            if "SessionExpired" in name or "Session expired" in message:
                raise ScanError("The stored session has expired. Re-authenticate:\n"
                                "  python HQ-Credentials-Manager.py ui")
            raise ScanError(f"{name or 'OdooError'} on {model}.{method}: {message}")
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


def load_practices(client: OdooSessionClient, projects: list[dict]) -> dict[int, dict]:
    """Project id -> the product category of the sale line that created it."""
    line_to_projects: dict[int, list[int]] = {}
    for project in projects:
        line_id = id_of(project.get("sale_line_id"))
        if line_id:
            line_to_projects.setdefault(line_id, []).append(project["id"])
    if not line_to_projects:
        return {}

    resolved: dict[int, dict] = {}
    lines = client.search_read("sale.order.line",
                               [["id", "in", list(line_to_projects)]],
                               fields=["id", "categ_id", "product_id"], batch=300)
    for line in lines:
        category_name = name_of(line.get("categ_id"))
        product_name = name_of(line.get("product_id"))
        generic = (category_name or "").strip().lower() in GENERIC_CATEGORIES
        practice = None if not category_name or generic else category_name
        for project_id in line_to_projects.get(line["id"], []):
            resolved[project_id] = {"practice": practice, "product": product_name,
                                    "category": category_name}
    missing = len(line_to_projects) - len(lines)
    if missing > 0:
        log.warning("%d sale order line(s) referenced by projects could not be read "
                    "(deleted, or hidden by a record rule)", missing)
    return resolved


def load_user_names(client: OdooSessionClient, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = client.search_read("res.users", [["id", "in", sorted(user_ids)]],
                              fields=["id", "name"], batch=300)
    return {row["id"]: row.get("name") for row in rows}


def derive_practice(project: dict, practices: dict) -> dict:
    entry = practices.get(project["id"])
    if entry and entry.get("practice"):
        return {"practice": entry["practice"], "source": "product_category",
                "detail": entry.get("product")}
    if entry and entry.get("product"):
        return {"practice": None, "source": "uncategorized_product",
                "detail": f"{entry['product']} [category: "
                          f"{entry.get('category') or 'none'}]"}
    return {"practice": None, "source": "no_sale_order", "detail": None}


def select_projects(all_projects: list[dict], practices: dict, practice: str,
                    limit: int | None) -> tuple[list[dict], dict[str, int]]:
    """Projects in the wanted practice, plus the practice tally over all of them."""
    wanted = practice.strip().lower()
    selected: list[dict] = []
    distribution: dict[str, int] = {}
    for project in all_projects:
        resolved = derive_practice(project, practices)
        key = resolved["practice"] or f"({resolved['source']})"
        distribution[key] = distribution.get(key, 0) + 1
        if wanted and (resolved["practice"] or "").lower() != wanted:
            continue
        if limit is not None and len(selected) >= limit:
            continue
        selected.append({
            "id": project["id"],
            "name": project.get("name"),
            "practice": resolved["practice"],
            "practice_source": resolved["source"],
            "practice_evidence": resolved["detail"],
            "active": bool(project.get("active", True)),
        })
    return selected, distribution


def load_tasks(client: OdooSessionClient, project_ids: list[int],
               include_archived: bool,
               include_subtasks: bool) -> tuple[dict[int, list[dict]], int]:
    if not project_ids:
        return {}, 0

    domain: list[Any] = [["project_id", "in", project_ids]]
    if include_archived:
        domain.append(["active", "in", [True, False]])
    if not include_subtasks:
        domain.append(["parent_id", "=", False])

    tasks = client.search_read("project.task", domain, fields=TASK_SCAN_FIELDS,
                               batch=500)
    user_names = load_user_names(
        client, {uid for task in tasks for uid in (task.get("user_ids") or [])})

    by_project: dict[int, list[dict]] = {}
    for task in tasks:
        project_id = id_of(task.get("project_id"))
        by_project.setdefault(project_id, []).append({
            "id": task["id"],
            "name": task.get("name"),
            "assignees": [user_names.get(uid) or f"user:{uid}"
                          for uid in (task.get("user_ids") or [])],
        })
    return by_project, len(tasks)


def scan(client: OdooSessionClient, practice: str = "Odoo",
         include_archived: bool = False, include_subtasks: bool = True,
         limit: int | None = None, out_dir: Path = OUT_DIR) -> dict:
    run_id = next_run_id("odoo")
    log.info("odoo scan %s: practice=%r archived=%s subtasks=%s",
             run_id, practice or "(all)", include_archived, include_subtasks)

    domain: list[Any] = [["active", "in", [True, False]]] if include_archived else []
    all_projects = client.search_read("project.project", domain,
                                      fields=PROJECT_SCAN_FIELDS, order="name asc")
    practices = load_practices(client, all_projects)
    log.info("%d projects, a practice resolved for %d",
             len(all_projects), len(practices))

    selected, distribution = select_projects(all_projects, practices, practice, limit)
    if practice.strip() and not selected:
        available = ", ".join(sorted(k for k in distribution if not k.startswith("(")))
        raise ScanError(f"No projects resolved to practice '{practice}'.\n"
                        f"Practices found: {available or '(none)'}")

    tasks_by_project, task_total = load_tasks(
        client, [project["id"] for project in selected],
        include_archived, include_subtasks)
    log.info("%d projects in practice %r, %d tasks",
             len(selected), practice or "all", task_total)

    document = {
        "scan": {
            "run_id": run_id,
            "scanned_at": now_iso(),
            "odoo_url": client.url,
            "db": client.db,
            "server_version": client.server_version,
            "scanned_by": {"uid": client.uid, "username": client.username},
            "practice": practice or "(all practices)",
            "practice_derived_from": "project.sale_line_id -> sale.order.line.categ_id",
            "include_archived": include_archived,
            "include_subtasks": include_subtasks,
            "project_count": len(selected),
            "task_count": task_total,
            "practice_distribution_all_projects": dict(
                sorted(distribution.items(), key=lambda kv: -kv[1])),
        },
        "projects": [
            {**project, "tasks": tasks_by_project.get(project["id"], [])}
            for project in selected
        ],
    }

    body = json.dumps(document, indent=2, default=str)
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = out_dir / ODOO_SCAN_FILE.name
    primary.write_text(body, encoding="utf-8")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    archive = HISTORY_DIR / f"odoo_{run_id}.json"
    archive.write_text(body, encoding="utf-8")
    log.info("written to %s (archived as %s)", primary.name, archive.name)

    return {"run_id": run_id, "practice": practice or "(all)",
            "projects": len(selected), "tasks": task_total,
            "file": str(primary), "archive": str(archive)}


def practice_overview(client: OdooSessionClient) -> str:
    projects = client.search_read("project.project", [["active", "in", [True, False]]],
                                  fields=PROJECT_SCAN_FIELDS)
    practices = load_practices(client, projects)

    tally: dict[str, int] = {}
    sources: dict[str, int] = {}
    uncategorized: dict[str, int] = {}
    for project in projects:
        resolved = derive_practice(project, practices)
        key = resolved["practice"] or "(no practice)"
        tally[key] = tally.get(key, 0) + 1
        sources[resolved["source"]] = sources.get(resolved["source"], 0) + 1
        if resolved["source"] == "uncategorized_product" and resolved["detail"]:
            uncategorized[resolved["detail"]] = uncategorized.get(
                resolved["detail"], 0) + 1

    lines = [f"Practices over {len(projects)} projects",
             "(product category on the project's sale order line: "
             "project.sale_line_id -> sale.order.line.categ_id)", ""]
    for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:>5}  {name}")
    lines.append("")
    lines.append("Resolved via: " + ", ".join(
        f"{k}={v}" for k, v in sorted(sources.items(), key=lambda kv: -kv[1])))
    if uncategorized:
        lines.append("")
        lines.append("Sold products in a catch-all category (no practice):")
        for name, count in sorted(uncategorized.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:>5}  {name}")
    return "\n".join(lines)


def practice_of(client: OdooSessionClient, query: str) -> str:
    domain: list[Any] = ([["id", "=", int(query)]] if query.strip().isdigit()
                         else [["name", "ilike", query]])
    domain.append(["active", "in", [True, False]])
    projects = client.search_read("project.project", domain,
                                  fields=PROJECT_SCAN_FIELDS, order="name asc")
    if not projects:
        return f"No project matches '{query}'."

    practices = load_practices(client, projects)
    lines = [f"{len(projects)} project(s) matching '{query}':", ""]
    for project in projects:
        resolved = derive_practice(project, practices)
        archived = "" if project.get("active", True) else "  (archived)"
        lines.append(f"  #{project['id']}  {project.get('name')}{archived}")
        lines.append(f"      practice: {resolved['practice'] or '(none)'}")
        lines.append(f"      from:     {resolved['source']}  {resolved['detail'] or ''}")
    return "\n".join(lines)


def cmd_practices(args: argparse.Namespace) -> int:
    client = OdooSessionClient.from_file()
    print(f"Reading practices from {client.url} (db {client.db})\n")
    print(practice_overview(client))
    return 0


def cmd_practice_of(args: argparse.Namespace) -> int:
    client = OdooSessionClient.from_file()
    print(practice_of(client, args.query))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    client = OdooSessionClient.from_file()
    log.info("scanning %s (db %s) as %s", client.url, client.db, client.username)
    result = scan(client, practice=args.practice or "",
                  include_archived=args.include_archived,
                  include_subtasks=not args.no_subtasks, limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="HQ-Scanner.py",
        description="Scan Odoo projects and tasks by practice using a stored session.")
    add_common_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("practices", help="list practices with project counts").set_defaults(
        func=cmd_practices)

    of_parser = sub.add_parser("practice-of",
                               help="show the practice of a project, by id or name")
    of_parser.add_argument("query", help="project id, or part of the project name")
    of_parser.set_defaults(func=cmd_practice_of)

    scan_parser = sub.add_parser("scan", help="scan one practice into JSON")
    scan_parser.add_argument("--practice", default="Odoo",
                             help="practice to scan; default: Odoo. "
                                  "Pass an empty string for every project.")
    scan_parser.add_argument("--include-archived", action="store_true")
    scan_parser.add_argument("--no-subtasks", action="store_true",
                             help="top-level tasks only")
    scan_parser.add_argument("--limit", type=int, default=None,
                             help="cap the project count for a trial run")
    scan_parser.set_defaults(func=cmd_scan)

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
