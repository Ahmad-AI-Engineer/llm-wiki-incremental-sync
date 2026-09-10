"""Compare the Odoo and Outline snapshots, and create the missing pages.

Create-only: this never edits or renames a page that already exists.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from hq_common import (HISTORY_DIR, MAPPING_FILE, ODOO_SCAN_FILE,
                       OUTLINE_SCAN_FILE, SYNC_LOG_FILE, HQError, OutlineClient,
                       add_common_arguments, configure_logging, env_positive_int,
                       get_logger, next_run_id, now_iso, outline_credentials)

SECTIONS = ["Overview", "Customer Introduction", "Tasks", "Roadmap",
            "Customizations", "Scope of Work", "Meetings"]

ODOO_ID_MARKER = "odoo-project-id"
DUPLICATE_THRESHOLD = 0.34
PREVIEW_ROWS = 12

STOPWORDS = {"the", "inc", "llc", "ltd", "co", "gmbh", "bv", "srl", "corp",
             "company", "odoo", "implementation", "project", "and", "of", "for",
             "v17", "v18", "v19", "pvt", "limited"}

log = get_logger("outline-sync")


class SyncError(HQError):
    pass


def normalize_title(title: str) -> str:
    """Drop the S00xxx sale-order prefix and punctuation, so the two systems'
    names for the same client can be compared."""
    without_order = re.sub(r"^S\d{4,6}\s*-\s*", "", str(title or "")).strip()
    return re.sub(r"[^a-z0-9]+", " ", without_order.lower()).strip()


def title_tokens(title: str) -> set[str]:
    return {token for token in normalize_title(title).split()
            if len(token) > 2 and token not in STOPWORDS}


def similarity(left: str, right: str) -> float:
    first, second = title_tokens(left), title_tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def nearest_titles(name: str, titles: list[str],
                   count: int = 3) -> list[tuple[float, str]]:
    scored = [(similarity(name, title), title) for title in titles]
    scored.sort(key=lambda pair: -pair[0])
    return [pair for pair in scored[:count] if pair[0] > 0]


def load_scan(path: Path = ODOO_SCAN_FILE) -> dict:
    if not path.exists():
        raise SyncError(f"No Odoo scan at {path}\n"
                        "Run first:  python HQ-Scanner.py scan --practice Odoo")
    return json.loads(path.read_text(encoding="utf-8"))


def load_outline_scan(path: Path = OUTLINE_SCAN_FILE) -> dict:
    if not path.exists():
        raise SyncError(f"No Outline snapshot at {path}\n"
                        "Run first:  python Outline-Scanner.py scan")
    return json.loads(path.read_text(encoding="utf-8"))


def load_mapping(path: Path = MAPPING_FILE) -> dict:
    if not path.exists():
        return {"projects": {}}
    mapping = json.loads(path.read_text(encoding="utf-8"))
    mapping.setdefault("projects", {})
    return mapping


def save_mapping(mapping: dict, path: Path = MAPPING_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, default=str), encoding="utf-8")


def append_sync_log(lines: list[str], path: Path = SYNC_LOG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "" if path.exists() else "# Outline sync log\n\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(header + "\n".join(lines) + "\n")


_reported_collisions: set[str] = set()


def index_pages(pages: list[dict]) -> tuple[dict, dict, dict]:
    """Index the Outline pages by odoo id marker, document id, and title.

    compare() runs more than once per pipeline run, so a title collision is
    reported only the first time it is seen.
    """
    by_marker = {page["odoo_project_id"]: page for page in pages
                 if page.get("odoo_project_id")}
    by_document_id = {page["id"]: page for page in pages}
    by_title: dict[str, dict] = {}
    for page in pages:
        key = normalize_title(page.get("title") or "")
        if not key:
            log.debug("Outline page %s has an empty title, skipped for matching",
                      page.get("id"))
        elif key in by_title:
            if key not in _reported_collisions:
                _reported_collisions.add(key)
                log.warning("two Outline pages normalize to %r: %r and %r", key,
                            by_title[key].get("title"), page.get("title"))
        else:
            by_title[key] = page
    return by_marker, by_document_id, by_title


def find_page(project: dict, by_marker: dict, by_document_id: dict,
              by_title: dict, mapped: dict) -> tuple[dict | None, str]:
    """The Outline page for an Odoo project, and how it was matched."""
    page = by_marker.get(project["id"])
    if page:
        return page, "marker"
    entry = mapped.get(str(project["id"]))
    if entry:
        page = by_document_id.get(entry.get("id"))
        if page:
            return page, "mapping"
    return by_title.get(normalize_title(project.get("name") or "")), "title"


def compare(odoo_scan: dict, outline_snapshot: dict, mapping: dict) -> dict:
    pages = outline_snapshot.get("projects") or []
    by_marker, by_document_id, by_title = index_pages(pages)
    all_titles = [str(page.get("title") or "") for page in pages]
    mapped = mapping.get("projects") or {}

    linked, title_matched, probable, odoo_only = [], [], [], []
    claimed: set[str] = set()

    for project in odoo_scan.get("projects") or []:
        page, how = find_page(project, by_marker, by_document_id, by_title, mapped)

        if page:
            claimed.add(page["id"])
            record = {
                "odoo_id": project["id"],
                "odoo_name": project.get("name"),
                "outline_title": page.get("title"),
                "outline_url": page.get("url"),
                "how": how,
                "odoo_tasks": len(project.get("tasks") or []),
                "outline_tasks": page.get("task_count") or 0,
                "missing_sections": [s for s in SECTIONS
                                     if s not in (page.get("section_titles") or [])],
            }
            (linked if how in ("marker", "mapping") else title_matched).append(record)
            continue

        near = nearest_titles(project.get("name") or "", all_titles)
        entry = {"odoo_id": project["id"], "odoo_name": project.get("name"),
                 "odoo_tasks": len(project.get("tasks") or [])}
        if near and near[0][0] >= DUPLICATE_THRESHOLD:
            probable.append({**entry, "score": near[0][0],
                             "outline_title": near[0][1]})
        else:
            odoo_only.append(entry)

    outline_only = [{"outline_title": page.get("title"), "outline_url": page.get("url"),
                     "sections": page.get("section_titles") or [],
                     "outline_tasks": page.get("task_count") or 0}
                    for page in pages if page["id"] not in claimed]

    drifted = [row for row in linked + title_matched
               if row["odoo_tasks"] != row["outline_tasks"] or row["missing_sections"]]

    return {"linked": linked, "title_matched": title_matched, "probable": probable,
            "odoo_only": odoo_only, "outline_only": outline_only, "drifted": drifted}


def escape_cell(value: Any) -> str:
    """One markdown table cell: no pipes, and no line breaks to split the row."""
    text = str(value if value is not None else "")
    return re.sub(r"\s+", " ", text.replace("|", r"\|")).strip()


def tasks_markdown(project: dict) -> str:
    tasks = project.get("tasks") or []
    if not tasks:
        return "_No tasks in Odoo for this project._"
    lines = ["| Task | Assignees |", "| --- | --- |"]
    for task in tasks:
        who = ", ".join(task.get("assignees") or []) or "—"
        lines.append(f"| {escape_cell(task.get('name'))} | {escape_cell(who)} |")
    lines.append("")
    lines.append(f"_{len(tasks)} task(s) from Odoo._")
    return "\n".join(lines)


def project_markdown(project: dict, scan_meta: dict) -> str:
    return "\n".join([
        f"<!-- {ODOO_ID_MARKER}: {project['id']} -->",
        "",
        "| | |",
        "| --- | --- |",
        f"| Odoo project | `#{project['id']}` {escape_cell(project.get('name'))} |",
        f"| Practice | {escape_cell(project.get('practice')) or '—'} |",
        f"| Sold as | {escape_cell(project.get('practice_evidence')) or '—'} |",
        f"| Odoo instance | {scan_meta.get('odoo_url')} |",
        f"| Last synced | {now_iso()} |",
        "",
        "_Synced from Odoo by HQ-Outline-Sync. Section pages are below._",
    ])


def plan_for(project: dict, scan_meta: dict) -> dict:
    """Everything that would be written for one project. Tasks is the only
    section with an Odoo source; the rest are created empty for humans."""
    return {
        "odoo_id": project["id"],
        "title": project.get("name"),
        "project_body": project_markdown(project, scan_meta),
        "sections": [
            {"title": section,
             "body": tasks_markdown(project) if section == "Tasks" else ""}
            for section in SECTIONS
        ],
        "task_count": len(project.get("tasks") or []),
    }


def create_project_page(client: OutlineClient, collection_id: str, plan: dict) -> dict:
    """Create the project page and its section pages.

    Outline inserts every new sibling at the top, so the sections are created
    in reverse to land in the intended order.
    """
    parent = client.create_document(plan["title"], plan["project_body"], collection_id)
    created = []
    for section in reversed(plan["sections"]):
        child = client.create_document(section["title"], section["body"],
                                       collection_id, parent["id"])
        created.append({"title": section["title"], "id": child["id"],
                        "url": child.get("url")})
    created.reverse()
    return {"document_id": parent["id"], "title": plan["title"],
            "url": parent.get("url"), "sections": created}


def live_titles(client: OutlineClient, collection_id: str) -> list[dict]:
    """Current top-level pages, fetched fresh in one call for the write-time guard."""
    return [{"id": node.get("id"), "title": str(node.get("title") or ""),
             "sections": [str(child.get("title"))
                          for child in (node.get("children") or [])]}
            for node in client.collection_tree(collection_id)]


def guard_duplicate(project: dict, pages: list[dict], force: bool) -> None:
    """Refuse to create when the live collection already has this project."""
    wanted = normalize_title(project.get("name") or "")
    exact = next((p for p in pages if normalize_title(p["title"]) == wanted), None)
    if exact and not force:
        raise SyncError(
            f"An Outline page already matches this project:\n"
            f"  Odoo:    {project.get('name')}\n"
            f"  Outline: {exact['title']} (sections: {exact['sections']})\n"
            "Create-only mode will not touch it. Use --force to create anyway.")

    near = nearest_titles(project.get("name") or "", [p["title"] for p in pages])
    if near and near[0][0] >= DUPLICATE_THRESHOLD and not force:
        listed = "\n".join(f"    ~{score:.2f}  {title}" for score, title in near)
        raise SyncError(
            f"No exact match, but similar Outline pages exist:\n{listed}\n"
            f"  Odoo: {project.get('name')}\n"
            "Creating now could duplicate one. Use --force if it is separate.")


def record_creation(mapping: dict, project: dict, created: dict, url: str,
                    collection: str) -> None:
    mapping.setdefault("projects", {})[str(project["id"])] = {
        "id": created["document_id"],
        "title": created["title"],
        "url": created.get("url"),
        "sections": {s["title"]: s["id"] for s in created["sections"]},
        "created_at": now_iso(),
        "odoo_name": project.get("name"),
    }
    mapping["outline_url"] = url
    mapping["collection_id"] = collection
    save_mapping(mapping)


def log_created(project: dict, created: dict, url: str, task_count: int) -> None:
    append_sync_log([f"- {now_iso()} CREATE odoo#{project['id']} "
                     f"{project.get('name')!r} -> {url}{created.get('url')} "
                     f"({task_count} tasks)"])


def resolve_cap(cli_limit: int | None) -> tuple[int | None, str | None]:
    """--limit wins over SYNC_CAP; 0 is a real value meaning create nothing."""
    if cli_limit is not None:
        return cli_limit, "--limit"
    try:
        cap = env_positive_int("SYNC_CAP")
    except ValueError as exc:
        raise SyncError(str(exc)) from None
    return cap, ("SYNC_CAP in .env" if cap is not None else None)


def cmd_status(args: argparse.Namespace) -> int:
    url, key, collection = outline_credentials()
    client = OutlineClient(url, key)
    who = client.whoami()
    user, team = who.get("user") or {}, who.get("team") or {}
    print(f"Outline: {team.get('name')} ({url})")
    print(f"  authenticated as {user.get('name')} <{user.get('email')}> "
          f"role={user.get('role')}")
    pages = live_titles(client, collection)
    print(f"  collection {collection}: {len(pages)} project pages (live)")

    odoo_scan = load_scan()
    outline_snapshot = load_outline_scan()
    odoo_meta = odoo_scan.get("scan") or {}
    outline_meta = outline_snapshot.get("scan") or {}
    print("\nSnapshots on disk")
    print(f"  odoo    run {odoo_meta.get('run_id')}  "
          f"{odoo_meta.get('project_count')} projects, "
          f"{odoo_meta.get('task_count')} tasks")
    print(f"  outline run {outline_meta.get('run_id')}  "
          f"{outline_meta.get('project_count')} project pages")

    cap, _ = resolve_cap(None)
    print(f"\n  SYNC_CAP: {cap if cap is not None else 'unset (no cap)'}")

    result = compare(odoo_scan, outline_snapshot, load_mapping())
    print(f"  to create: {len(result['odoo_only'])}   "
          f"needs review: {len(result['probable'])}   "
          f"drifted: {len(result['drifted'])}")
    return 0


def print_compare(result: dict, show: str, limit: int) -> None:
    if show in ("drift", "all"):
        print("\n-- matched pages whose content differs from Odoo --")
        for row in result["drifted"][:limit]:
            missing = (", missing: " + ", ".join(row["missing_sections"])
                       if row["missing_sections"] else "")
            print(f"  odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:32]:<34} "
                  f"odoo={row['odoo_tasks']:>4} outline={row['outline_tasks']:>4}"
                  f"{missing[:64]}")
    if show in ("probable", "all"):
        print("\n-- probable matches, not linked (review before creating) --")
        for row in result["probable"][:limit]:
            print(f"  odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:32]:<34} "
                  f"~{row['score']:.2f}  {row['outline_title']}")
    if show in ("odoo-only", "all"):
        print("\n-- in Odoo, no Outline page --")
        for row in sorted(result["odoo_only"], key=lambda r: -r["odoo_tasks"])[:limit]:
            print(f"  odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:42]:<44} "
                  f"{row['odoo_tasks']:>4} tasks")
    if show in ("outline-only", "all"):
        print("\n-- in Outline, no Odoo-practice project --")
        for row in result["outline_only"][:limit]:
            print(f"  {str(row['outline_title'])[:42]:<44} "
                  f"sections={len(row['sections']):<3} tasks={row['outline_tasks']}")


def cmd_compare(args: argparse.Namespace) -> int:
    odoo_scan = load_scan()
    outline_snapshot = load_outline_scan()
    result = compare(odoo_scan, outline_snapshot, load_mapping())

    odoo_meta = odoo_scan.get("scan") or {}
    outline_meta = outline_snapshot.get("scan") or {}
    print(f"Odoo    run {odoo_meta.get('run_id')}  practice={odoo_meta.get('practice')}  "
          f"{odoo_meta.get('project_count')} projects, {odoo_meta.get('task_count')} tasks")
    print(f"Outline run {outline_meta.get('run_id')}  "
          f"{outline_meta.get('collection_name')!r}  "
          f"{outline_meta.get('project_count')} project pages")

    print(f"\n  linked by id marker/mapping  : {len(result['linked']):>4}")
    print(f"  matched by title only        : {len(result['title_matched']):>4}")
    print(f"  probable match, needs review : {len(result['probable']):>4}")
    print(f"  in Odoo only  (to create)    : {len(result['odoo_only']):>4}")
    print(f"  in Outline only (unmatched)  : {len(result['outline_only']):>4}")
    print(f"  matched but drifted          : {len(result['drifted']):>4}")

    print_compare(result, args.show, args.limit)
    if args.show == "none":
        print("\n  (--show drift|probable|odoo-only|outline-only|all for detail)")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    result = compare(load_scan(), load_outline_scan(), load_mapping())
    rows = sorted(result["odoo_only"], key=lambda r: -r["odoo_tasks"])
    print(f"{len(rows)} Odoo projects with no Outline page and no similar title:\n")
    for row in rows[:args.limit]:
        print(f"  odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:44]:<46} "
              f"{row['odoo_tasks']:>4} tasks")
    print(f"\n{len(result['probable'])} excluded as probable duplicates "
          f"(see 'compare --show probable')")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    url, key, collection = outline_credentials()
    client = OutlineClient(url, key)
    odoo_scan = load_scan()
    scan_meta = odoo_scan.get("scan") or {}

    projects = {str(p["id"]): p for p in (odoo_scan.get("projects") or [])}
    project = projects.get(str(args.project))
    if not project:
        raise SyncError(f"Project {args.project} is not in the current scan "
                        f"({len(projects)} projects). Check with 'candidates'.")

    mapping = load_mapping()
    if str(project["id"]) in mapping["projects"]:
        raise SyncError(f"Project {project['id']} is already mapped to an Outline "
                        "document. This tool is create-only.")

    guard_duplicate(project, live_titles(client, collection), args.force)

    plan = plan_for(project, scan_meta)
    print(f"Project #{plan['odoo_id']}: {plan['title']}")
    print(f"  practice: {project.get('practice')}   tasks: {plan['task_count']}")
    print(f"  would create 1 project page + {len(plan['sections'])} section pages")
    for section in plan["sections"]:
        first = section["body"].splitlines()[0] if section["body"] else "(empty)"
        print(f"     - {section['title']:<24} {first[:52]}")

    if args.dry_run:
        print("\nDRY RUN. Nothing written.")
        return 0

    log.info("creating Outline page for odoo#%s %r", project["id"], project.get("name"))
    created = create_project_page(client, collection, plan)
    record_creation(mapping, project, created, url, collection)
    log_created(project, created, url, plan["task_count"])
    print(f"\n  created: {url}{created.get('url')}")
    for section in created["sections"]:
        print(f"     {section['title']:<24} {url}{section.get('url')}")
    log.info("mapping saved to %s", MAPPING_FILE)
    return 0


def print_sync_plan(run_id: str, scan_meta: dict, outline_meta: dict, result: dict,
                    todo: list[dict], available: int, cap: int | None,
                    cap_source: str | None, include_probable: bool) -> None:
    print(f"Sync run {run_id}")
    print(f"  odoo scan run    {scan_meta.get('run_id')}")
    print(f"  outline snapshot {outline_meta.get('run_id')}")
    print(f"\n  {len(result['odoo_only'])} projects have no Outline page")
    if not include_probable:
        print(f"  {len(result['probable'])} probable duplicates excluded "
              "(--include-probable to force)")
    if cap is None:
        print(f"  no cap set -- all {available} would be created")
    else:
        print(f"  capped at {cap} of {available} by {cap_source}")
    print(f"\n  would create {len(todo)} project pages "
          f"= {len(todo) * (1 + len(SECTIONS))} documents")
    for row in todo[:PREVIEW_ROWS]:
        print(f"     odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:42]:<44} "
              f"{row['odoo_tasks']:>4} tasks")
    if len(todo) > PREVIEW_ROWS:
        print(f"     ... and {len(todo) - PREVIEW_ROWS} more")


def cmd_sync_all(args: argparse.Namespace) -> int:
    url, key, collection = outline_credentials()
    client = OutlineClient(url, key)
    odoo_scan = load_scan()
    scan_meta = odoo_scan.get("scan") or {}
    outline_snapshot = load_outline_scan()
    outline_meta = outline_snapshot.get("scan") or {}
    mapping = load_mapping()

    projects = {p["id"]: p for p in (odoo_scan.get("projects") or [])}
    result = compare(odoo_scan, outline_snapshot, mapping)

    run_id = next_run_id("sync")
    todo = list(result["odoo_only"])
    if args.include_probable:
        todo += result["probable"]
    todo.sort(key=lambda row: -row["odoo_tasks"])

    available = len(todo)
    cap, cap_source = resolve_cap(args.limit)
    if cap is not None:
        todo = todo[:cap]

    if not getattr(args, "quiet", False):
        print_sync_plan(run_id, scan_meta, outline_meta, result, todo, available,
                        cap, cap_source, args.include_probable)

    if not todo:
        print("\nNothing to do.")
        return 0
    if args.dry_run:
        print("\nDRY RUN. Nothing written.")
        return 0

    pages = live_titles(client, collection)
    log.info("sync run %s: creating %d project pages", run_id, len(todo))
    created_records: list[dict] = []
    failures: list[dict] = []

    for index, row in enumerate(todo, start=1):
        project = projects.get(row["odoo_id"])
        if not project:
            log.warning("odoo#%s vanished from the scan, skipped", row["odoo_id"])
            continue
        try:
            guard_duplicate(project, pages, args.include_probable)
            plan = plan_for(project, scan_meta)
            created = create_project_page(client, collection, plan)
            record_creation(mapping, project, created, url, collection)
            log_created(project, created, url, plan["task_count"])
            created_records.append({
                "odoo_id": project["id"],
                "odoo_name": project.get("name"),
                "outline_document_id": created["document_id"],
                "outline_url": f"{url}{created.get('url')}",
                "task_count": plan["task_count"],
                "sections": [s["title"] for s in created["sections"]],
                "created_at": now_iso(),
            })
            pages.append({"id": created["document_id"], "title": plan["title"],
                          "sections": SECTIONS})
            log.info("[%d/%d] odoo#%s %s -> %s", index, len(todo), project["id"],
                     str(project.get("name"))[:40], created.get("url"))
        except HQError as exc:
            failures.append({"odoo_id": project["id"],
                             "odoo_name": project.get("name"), "error": str(exc)})
            log.error("[%d/%d] odoo#%s failed: %s", index, len(todo), project["id"],
                      str(exc).splitlines()[0][:140])
        if args.delay:
            time.sleep(args.delay)

    record = {
        "sync": {
            "run_id": run_id,
            "finished_at": now_iso(),
            "outline_url": url,
            "collection_id": collection,
            "odoo_scan_run": scan_meta.get("run_id"),
            "outline_scan_run": outline_meta.get("run_id"),
            "practice": scan_meta.get("practice"),
            "available_to_create": available,
            "cap": cap,
            "capped_by": cap_source,
            "attempted": len(todo),
            "created": len(created_records),
            "failed": len(failures),
        },
        "created": created_records,
        "failures": failures,
    }
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    record_path = HISTORY_DIR / f"sync_{run_id}.json"
    record_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    if failures:
        append_sync_log([f"- {now_iso()} FAILED odoo#{f['odoo_id']}: "
                         f"{f['error'].splitlines()[0][:160]}" for f in failures])
    log.info("created %d, failed %d; run record %s",
             len(created_records), len(failures), record_path.name)
    print("\n  refresh the Outline snapshot:  python Outline-Scanner.py scan")
    return 1 if failures and not created_records else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="HQ-Outline-Sync.py",
        description="Compare the Odoo and Outline snapshots, and create missing "
                    "Outline project pages. Create-only.")
    add_common_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="connection, snapshots and cap").set_defaults(
        func=cmd_status)

    compare_parser = sub.add_parser("compare",
                                    help="compare both snapshots (offline)")
    compare_parser.add_argument("--show", default="none",
                                choices=["none", "drift", "probable", "odoo-only",
                                         "outline-only", "all"])
    compare_parser.add_argument("--limit", type=int, default=20)
    compare_parser.set_defaults(func=cmd_compare)

    candidates = sub.add_parser("candidates", help="projects safe to create")
    candidates.add_argument("--limit", type=int, default=25)
    candidates.set_defaults(func=cmd_candidates)

    sync = sub.add_parser("sync", help="create the page for one Odoo project")
    sync.add_argument("project", help="Odoo project id")
    sync.add_argument("--dry-run", action="store_true",
                      help="plan only, write nothing")
    sync.add_argument("--force", action="store_true",
                      help="create even if a similar page exists")
    sync.set_defaults(func=cmd_sync)

    sync_all = sub.add_parser("sync-all",
                              help="create pages for every project that has none")
    sync_all.add_argument("--dry-run", action="store_true",
                          help="plan only, write nothing")
    sync_all.add_argument("--limit", type=int, default=None,
                          help="cap this run, overriding SYNC_CAP")
    sync_all.add_argument("--delay", type=float, default=0.3,
                          help="seconds between document creates")
    sync_all.add_argument("--include-probable", action="store_true",
                          help="also create for probable duplicates")
    sync_all.set_defaults(func=cmd_sync_all)

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
