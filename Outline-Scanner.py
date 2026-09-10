"""Snapshot the Outline project collection to JSON."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from hq_common import (HISTORY_DIR, OUT_DIR, OUTLINE_SCAN_FILE, HQError,
                       OutlineClient, add_common_arguments, configure_logging,
                       get_logger, next_run_id, now_iso, outline_credentials)

ODOO_ID_PATTERN = re.compile(r"odoo-project-id:\s*(\d+)")
TASK_SECTION_TITLES = {"tasks", "task", "task list", "tasks detail",
                       "operational tasks"}
MAX_TREE_DEPTH = 2

log = get_logger("outline-scanner")


class OutlineScanError(HQError):
    pass


def table_first_column(text: str) -> list[str]:
    """First cell of every markdown table row, skipping header and separator."""
    values = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        if set(cells[0]) <= {"-", ":"} or cells[0].lower() == "task":
            continue
        values.append(cells[0])
    return values


def build_tree(documents: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Split the flat document list into top-level pages and a child index.

    A document whose parent is outside this collection counts as top-level,
    rather than being dropped from the snapshot entirely.
    """
    by_id = {doc["id"]: doc for doc in documents}
    children: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for doc in documents:
        parent = doc.get("parentDocumentId")
        if parent and parent in by_id:
            children.setdefault(parent, []).append(doc)
        elif parent:
            log.warning("document %r has a parent outside the collection; "
                        "counted as a top-level page", doc.get("title"))
            roots.append(doc)
        else:
            roots.append(doc)
    return roots, children


def describe(doc: dict, children: dict[str, list[dict]], depth: int = 0) -> dict:
    kids = children.get(doc["id"]) or []
    return {
        "id": doc["id"],
        "title": doc.get("title"),
        "url": doc.get("url"),
        "updated_at": doc.get("updatedAt"),
        "text_length": len(doc.get("text") or ""),
        "child_count": len(kids),
        "children": ([describe(kid, children, depth + 1) for kid in kids]
                     if depth < MAX_TREE_DEPTH else []),
    }


def task_names_for(sections: list[dict], by_id: dict[str, dict]) -> list[str]:
    """Task names under a section titled like Tasks: table rows and child pages."""
    names: list[str] = []
    for section in sections:
        if str(section["title"] or "").strip().lower() not in TASK_SECTION_TITLES:
            continue
        section_doc = by_id.get(section["id"]) or {}
        names.extend(table_first_column(section_doc.get("text") or ""))
        names.extend(str(child["title"]) for child in section["children"]
                     if child.get("title"))
    return names


def scan(client: OutlineClient, collection_id: str,
         out_dir: Path = OUT_DIR) -> dict:
    run_id = next_run_id("outline")
    who = client.whoami()
    collection = client.collection_info(collection_id)
    log.info("outline scan %s: collection %r", run_id, collection.get("name"))

    documents = client.all_documents(collection_id)
    by_id = {doc["id"]: doc for doc in documents}
    roots, children = build_tree(documents)

    projects = []
    for root in sorted(roots, key=lambda doc: str(doc.get("title") or "")):
        sections = [describe(kid, children)
                    for kid in (children.get(root["id"]) or [])]
        marker = ODOO_ID_PATTERN.search(root.get("text") or "")
        task_names = task_names_for(sections, by_id)
        projects.append({
            "id": root["id"],
            "title": root.get("title"),
            "url": root.get("url"),
            "updated_at": root.get("updatedAt"),
            "odoo_project_id": int(marker.group(1)) if marker else None,
            "section_titles": [section["title"] for section in sections],
            "sections": sections,
            "task_names": task_names,
            "task_count": len(task_names),
        })

    marked = sum(1 for project in projects if project["odoo_project_id"])
    log.info("%d documents, %d project pages, %d carry an odoo-project-id marker",
             len(documents), len(projects), marked)

    vocabulary: dict[str, int] = {}
    for project in projects:
        for title in project["section_titles"]:
            vocabulary[str(title)] = vocabulary.get(str(title), 0) + 1

    document = {
        "scan": {
            "run_id": run_id,
            "scanned_at": now_iso(),
            "outline_url": client.url,
            "collection_id": collection_id,
            "collection_name": collection.get("name"),
            "scanned_by": {"name": (who.get("user") or {}).get("name"),
                           "email": (who.get("user") or {}).get("email")},
            "document_count": len(documents),
            "project_count": len(projects),
            "synced_by_pipeline": marked,
            "task_names_note": "counted from markdown tables and child document "
                               "titles under a section named like Tasks; a page "
                               "whose tasks are prose reads as 0",
        },
        "section_vocabulary": dict(sorted(vocabulary.items(), key=lambda kv: -kv[1])),
        "projects": projects,
    }

    body = json.dumps(document, indent=2, default=str)
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = out_dir / OUTLINE_SCAN_FILE.name
    primary.write_text(body, encoding="utf-8")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    archive = HISTORY_DIR / f"outline_{run_id}.json"
    archive.write_text(body, encoding="utf-8")
    log.info("written to %s (archived as %s)", primary.name, archive.name)
    return document


def cmd_scan(args: argparse.Namespace) -> int:
    url, key, collection = outline_credentials()
    document = scan(OutlineClient(url, key), collection)
    print(json.dumps(document["scan"], indent=2))
    return 0


def cmd_sections(args: argparse.Namespace) -> int:
    if not OUTLINE_SCAN_FILE.exists():
        raise OutlineScanError(f"No Outline snapshot at {OUTLINE_SCAN_FILE}\n"
                               "Run first:  python Outline-Scanner.py scan")
    document = json.loads(OUTLINE_SCAN_FILE.read_text(encoding="utf-8"))
    vocabulary = document.get("section_vocabulary") or {}
    print(f"Section titles across {document['scan']['project_count']} "
          f"project pages:\n")
    for title, count in list(vocabulary.items())[:args.limit]:
        print(f"  {count:>4}  {title!r}")
    singles = sum(1 for count in vocabulary.values() if count == 1)
    print(f"\n  {len(vocabulary)} distinct section titles, {singles} used once")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="Outline-Scanner.py",
        description="Snapshot the Outline project collection to JSON.")
    add_common_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="snapshot the collection").set_defaults(func=cmd_scan)

    sections = sub.add_parser("sections",
                              help="section-title vocabulary from the snapshot")
    sections.add_argument("--limit", type=int, default=40)
    sections.set_defaults(func=cmd_sections)

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
