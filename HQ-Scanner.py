from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any
from hq_common import (HISTORY_DIR, ODOO_SCAN_FILE, OUT_DIR, HQError,
                       OdooSessionClient, add_common_arguments,
                       configure_logging, get_logger, next_run_id, now_iso)

PROJECT_SCAN_FIELDS = ["id", "name", "active", "sale_line_id"]
TASK_SCAN_FIELDS = ["id", "name", "project_id", "user_ids"]
TASK_SYNC_FIELDS = ["id", "name", "project_id", "user_ids", "write_date"]

GENERIC_CATEGORIES = {"all", "all / saleable", "all / expenses", "all / deliveries",
                      "all / saleable / events", "goods", "services"}

log = get_logger("odoo-scanner")


class ScanError(HQError):
    pass


def name_of(value: Any) -> str | None:
    return value[1] if isinstance(value, list) and len(value) > 1 else None


def id_of(value: Any) -> int | None:
    return value[0] if isinstance(value, list) and value else None


def load_practices(client: OdooSessionClient, projects: list[dict]) -> dict[int, dict]:
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
        category = name_of(line.get("categ_id"))
        if not category or category.strip().lower() in GENERIC_CATEGORIES:
            continue
        for project_id in line_to_projects.get(line["id"], []):
            resolved[project_id] = {"practice": category,
                                    "product": name_of(line.get("product_id"))}
    missing = len(line_to_projects) - len(lines)
    if missing:
        log.warning("%d sale order line(s) could not be read (deleted, or hidden "
                    "by a record rule)", missing)
    return resolved


def load_user_names(client: OdooSessionClient, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = client.search_read("res.users",
                              [["id", "in", sorted(user_ids)],
                               ["active", "in", [True, False]]],
                              fields=["id", "name"], batch=300)
    names = {row["id"]: row.get("name") for row in rows}
    missing = len(user_ids) - len(names)
    if missing:
        log.warning("%d assignee(s) could not be named and will show as user:<id>",
                    missing)
    return names


def select_projects(all_projects: list[dict], practices: dict, practice: str,
                    limit: int | None) -> tuple[list[dict], dict[str, int]]:
    wanted = practice.strip().lower()
    selected: list[dict] = []
    distribution: dict[str, int] = {}
    for project in all_projects:
        entry = practices.get(project["id"])
        if not entry:
            continue
        name = entry["practice"]
        distribution[name] = distribution.get(name, 0) + 1
        if wanted and name.lower() != wanted:
            continue
        if limit is not None and len(selected) >= limit:
            continue
        selected.append({
            "id": project["id"],
            "name": project.get("name"),
            "practice": name,
            "practice_evidence": entry["product"],
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


def list_projects(client: OdooSessionClient, practice: str,
                  include_archived: bool) -> list[dict]:
    domain: list[Any] = [["active", "in", [True, False]]] if include_archived else []
    projects = client.search_read("project.project", domain,
                                  fields=PROJECT_SCAN_FIELDS, order="name asc")
    selected, _ = select_projects(projects, load_practices(client, projects),
                                  practice, None)
    return selected


def fetch_tasks(client: OdooSessionClient, project_ids: list[int],
                include_archived: bool, since: str | None = None) -> list[dict]:
    if not project_ids:
        return []
    domain: list[Any] = [["project_id", "in", project_ids]]
    if since:
        domain.append(["write_date", ">", since])
    if include_archived:
        domain.append(["active", "in", [True, False]])
    rows = client.search_read("project.task", domain, fields=TASK_SYNC_FIELDS,
                              batch=500)
    names = load_user_names(
        client, {uid for row in rows for uid in (row.get("user_ids") or [])})
    return [{"id": row["id"],
             "name": row.get("name"),
             "project_id": id_of(row.get("project_id")),
             "write_date": row["write_date"],
             "assignees": [names.get(uid) or f"user:{uid}"
                           for uid in (row.get("user_ids") or [])]}
            for row in rows]


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
    selected, distribution = select_projects(all_projects, practices, practice, limit)
    if not selected:
        if not practices:
            raise ScanError("No customer projects found: no project has a sale "
                            "order line in a practice category.")
        raise ScanError(f"No customer projects in practice '{practice}'.\n"
                        f"Practices found: {', '.join(sorted(distribution))}")
    log.info("%d projects, %d for customers, %d in practice %r",
             len(all_projects), len(practices), len(selected), practice or "any")

    tasks_by_project, task_total = load_tasks(
        client, [project["id"] for project in selected],
        include_archived, include_subtasks)

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
            "projects_examined": len(all_projects),
            "customer_project_count": len(practices),
            "project_count": len(selected),
            "task_count": task_total,
            "practice_distribution": dict(
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
    log.info("%d tasks; written to %s (archived as %s)",
             task_total, primary.name, archive.name)

    return {"run_id": run_id, "practice": practice or "(all)",
            "projects": len(selected), "tasks": task_total,
            "file": str(primary), "archive": str(archive)}


def practice_overview(client: OdooSessionClient) -> str:
    projects = client.search_read("project.project", [["active", "in", [True, False]]],
                                  fields=PROJECT_SCAN_FIELDS)
    practices = load_practices(client, projects)

    tally: dict[str, int] = {}
    for entry in practices.values():
        tally[entry["practice"]] = tally.get(entry["practice"], 0) + 1

    lines = [f"Customer practices over {len(projects)} projects",
             "(product category on the project's sale order line: "
             "project.sale_line_id -> sale.order.line.categ_id)", ""]
    for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:>5}  {name}")
    lines.append("")
    lines.append(f"  {len(projects) - len(practices)} internal projects have no "
                 "practice and are out of scope")
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
        entry = practices.get(project["id"])
        archived = "" if project.get("active", True) else "  (archived)"
        lines.append(f"  #{project['id']}  {project.get('name')}{archived}")
        if entry:
            lines.append(f"      practice: {entry['practice']}")
            lines.append(f"      sold as:  {entry['product'] or '-'}")
        else:
            lines.append("      practice: none, internal project, out of scope")
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
