from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from hq_common import BASE_DIR, read_env

DEFAULT_STATE_FILE = "output/state.json"

PROJECT_CATEGORY = "Project"
TASKS_SECTION = "Tasks"
TASK_CATEGORY = "Task"
SYNCED_STATUS = 1

TABLES = ["collections", "categories", "page_statuses", "pages", "sources",
          "page_sources", "routing_hints", "routing_hint_pages", "lint_findings"]


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class State:

    def __init__(self, path: Path) -> None:
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.tables = {name: data.get(name) or [] for name in TABLES}
        if not self.tables["page_statuses"]:
            self.tables["page_statuses"].append({
                "status_id": SYNCED_STATUS, "code": "synced",
                "description": "Written by the sync from Odoo"})
        self.pages_by_id = {page["page_id"]: page for page in self.tables["pages"]}
        self.pages_by_document = {page["atlas_document_id"]: page
                                  for page in self.tables["pages"]}
        self.sources_by_key = {(source["source_type"], source["reference"]): source
                               for source in self.tables["sources"]}
        self.page_of_source = {link["source_id"]: link["page_id"]
                               for link in self.tables["page_sources"]}

    @classmethod
    def load(cls) -> "State":
        return cls(BASE_DIR / (read_env().get("STATE_FILE") or DEFAULT_STATE_FILE))

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(self.tables, indent=2), encoding="utf-8")
        staging.replace(self.path)

    def category_id(self, name: str) -> int:
        return next(row["category_id"] for row in self.tables["categories"]
                    if row["name"] == name)

    def ensure_collection(self, collection_id: str, name: str, created_at: str,
                          sections: list[str]) -> None:
        if not any(row["collection_id"] == collection_id
                   for row in self.tables["collections"]):
            self.tables["collections"].append({"collection_id": collection_id,
                                               "name": name,
                                               "created_at": created_at})
        known = {row["name"] for row in self.tables["categories"]}
        for order, category in enumerate([PROJECT_CATEGORY, *sections, TASK_CATEGORY]):
            if category not in known:
                self.tables["categories"].append({
                    "category_id": len(self.tables["categories"]) + 1,
                    "collection_id": collection_id,
                    "name": category,
                    "sort_order": order})
        self.write()

    def managed_projects(self) -> dict[int, dict]:
        tasks_category = self.category_id(TASKS_SECTION)
        tasks_pages = {page["parent_page_id"]: page for page in self.tables["pages"]
                       if page["category_id"] == tasks_category}
        managed = {}
        for (source_type, reference), source in self.sources_by_key.items():
            if source_type != "odoo_project":
                continue
            page = self.pages_by_id[self.page_of_source[source["source_id"]]]
            tasks = tasks_pages.get(page["page_id"])
            managed[int(reference)] = {
                "page_id": page["page_id"],
                "document_id": page["atlas_document_id"],
                "title": page["title"],
                "watermark": parse_time(page["last_synced_at"]),
                "tasks_page_id": tasks["page_id"] if tasks else None,
                "tasks_document_id": tasks["atlas_document_id"] if tasks else None,
            }
        return managed

    def task_pages(self, task_ids: list[int]) -> dict[int, dict]:
        found = {}
        for task_id in task_ids:
            source = self.sources_by_key.get(("odoo_task", str(task_id)))
            if source:
                page = self.pages_by_id[self.page_of_source[source["source_id"]]]
                found[task_id] = {"document_id": page["atlas_document_id"],
                                  "updated_at": parse_time(page["atlas_updated_at"])}
        return found

    def save_page(self, document: dict, category: str, parent_page_id: str | None,
                  synced_at: datetime | None,
                  source: tuple[str, int] | None = None) -> str:
        page = self.pages_by_document.get(document["id"])
        if page is None:
            page = {"page_id": str(uuid.uuid4()),
                    "atlas_document_id": document["id"],
                    "summary": None,
                    "category_id": self.category_id(category),
                    "parent_page_id": parent_page_id,
                    "status_id": SYNCED_STATUS,
                    "last_synced_at": None}
            self.tables["pages"].append(page)
            self.pages_by_id[page["page_id"]] = page
            self.pages_by_document[document["id"]] = page
        page["url_id"] = document["urlId"]
        page["title"] = document["title"]
        page["atlas_updated_at"] = document.get("updatedAt")
        if synced_at:
            page["last_synced_at"] = synced_at.isoformat()
        if source:
            self.link_source(page["page_id"], *source)
        return page["page_id"]

    def link_source(self, page_id: str, source_type: str, reference: int) -> None:
        key = (source_type, str(reference))
        source = self.sources_by_key.get(key)
        if source is None:
            source = {"source_id": len(self.tables["sources"]) + 1,
                      "source_type": source_type, "reference": key[1]}
            self.tables["sources"].append(source)
            self.sources_by_key[key] = source
        if source["source_id"] not in self.page_of_source:
            self.tables["page_sources"].append({"page_id": page_id,
                                                "source_id": source["source_id"]})
            self.page_of_source[source["source_id"]] = page_id

    def mark_synced(self, page_id: str, synced_at: datetime) -> None:
        self.pages_by_id[page_id]["last_synced_at"] = synced_at.isoformat()

    def record_finding(self, collection_id: str, severity: str,
                       description: str, found_at: datetime) -> None:
        findings = self.tables["lint_findings"]
        if any(row["description"] == description and row["resolved_at"] is None
               for row in findings):
            return
        findings.append({"finding_id": len(findings) + 1,
                         "collection_id": collection_id,
                         "page_id": None,
                         "severity": severity,
                         "description": description,
                         "found_at": found_at.isoformat(),
                         "resolved_at": None})
