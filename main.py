from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

from hq_common import (BASE_DIR, CLOCK_SKEW_SECONDS, HISTORY_DIR, HQError,
                       OdooSessionClient, OutlineClient, add_common_arguments,
                       clean_title, configure_logging, env_positive_int,
                       get_logger, next_run_id, now_iso, outline_credentials)
from hq_state import State, parse_time

PREVIEW_ROWS = 5

log = get_logger("pipeline")


class PipelineError(HQError):
    pass


def load_script(filename: str) -> ModuleType:
    path = BASE_DIR / filename
    if not path.exists():
        raise PipelineError(f"Missing script: {path}")
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def step_session(credentials: ModuleType, open_browser: bool) -> dict:
    log.info("STEP 1/4  checking the Odoo session")
    try:
        return credentials.ensure_session(open_browser=open_browser)
    except HQError as exc:
        raise PipelineError(f"Odoo login failed: {exc}") from None


def sync_cap() -> int | None:
    try:
        return env_positive_int("SYNC_CAP")
    except ValueError as exc:
        raise PipelineError(str(exc)) from None


def step_list(scanner: ModuleType, outline: ModuleType, sync: ModuleType,
              odoo: OdooSessionClient, client: OutlineClient, state: State,
              collection_id: str, args: argparse.Namespace) -> tuple[dict, dict]:
    log.info("STEP 2/4  listing projects in Odoo and Atlas")
    collection = client.collection_info(collection_id)
    state.ensure_collection(collection_id, collection["name"],
                            collection["createdAt"], sync.SECTIONS)
    projects = scanner.list_projects(odoo, args.practice, args.include_archived)
    roots = outline.list_roots(client, collection_id)
    managed = state.managed_projects()
    found = sync.reconcile(projects, roots, managed)
    log.info("odoo %d projects, atlas %d project pages, tracked %d, to adopt %d, "
             "to create %d", len(projects), len(roots), len(found["managed"]),
             len(found["adopt"]), len(found["create"]))
    return managed, found


def step_changes(scanner: ModuleType, sync: ModuleType, odoo: OdooSessionClient,
                 client: OutlineClient, state: State, collection_id: str,
                 args: argparse.Namespace, managed: dict, found: dict) -> dict:
    log.info("STEP 3/4  finding what changed")
    cap = sync_cap()
    create = found["create"] if cap is None else found["create"][:cap]
    tasks_of: dict[int, list[dict]] = {}
    for task in scanner.fetch_tasks(odoo, [project["id"] for project in create],
                                    args.include_archived):
        tasks_of.setdefault(task["project_id"], []).append(task)
    for project in create:
        project["tasks"] = tasks_of.get(project["id"], [])

    trees = {project["id"]: sync.read_tree(client, collection_id, root)
             for project, root in found["adopt"]}
    tracked = {project["id"]: project for project in found["managed"]}
    tracked.update({project["id"]: project for project, _ in found["adopt"]})

    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    since = {project_id: managed[project_id]["watermark"] - skew
             for project_id in tracked
             if project_id in managed and managed[project_id]["watermark"]}
    oldest = min(since.values(), default=None)
    tasks = scanner.fetch_tasks(odoo, list(since), args.include_archived,
                                oldest.strftime(sync.ODOO_TIME) if oldest else None)
    tasks += scanner.fetch_tasks(odoo, [project_id for project_id in tracked
                                        if project_id not in since],
                                 args.include_archived)

    pages = state.task_pages([task["id"] for task in tasks])
    for tree in trees.values():
        pages.update({task_id: {"document_id": document["id"],
                                "updated_at": parse_time(document["updatedAt"])}
                      for task_id, document in tree["tasks"].items()})

    titles = {project_id: entry["title"] for project_id, entry in managed.items()}
    titles.update({project["id"]: root.get("title")
                   for project, root in found["adopt"]})
    renames = {project_id for project_id, project in tracked.items()
               if clean_title(project.get("name")) != titles[project_id]}

    return {"create": create,
            "available": len(found["create"]),
            "cap": cap,
            "trees": trees,
            "tracked": tracked,
            "renames": renames,
            "changes": sync.task_changes(tasks, pages, since),
            "findings": sync.findings_for(found)}


def plan_counts(plan: dict) -> dict:
    return {
        "created_projects": len(plan["create"]),
        "created_project_tasks": sum(len(project["tasks"])
                                     for project in plan["create"]),
        "adopted": len(plan["trees"]),
        "renamed": len(plan["renames"]),
        "new_tasks": sum(len(change["create"]) for change in plan["changes"].values()),
        "updated_tasks": sum(len(change["update"])
                             for change in plan["changes"].values()),
        "findings": len(plan["findings"]),
    }


def report_plan(plan: dict, counts: dict) -> None:
    capped = (f" (SYNC_CAP {plan['cap']})"
              if plan["cap"] is not None and plan["cap"] < plan["available"] else "")
    print("\nPlan")
    print(f"  new projects to create in Atlas : {counts['created_projects']} of "
          f"{plan['available']}{capped}, {counts['created_project_tasks']} task subpages")
    print(f"  existing pages taken into state : {counts['adopted']}")
    print(f"  projects renamed in Odoo        : {counts['renamed']}")
    print(f"  new task pages                  : {counts['new_tasks']}")
    print(f"  task pages to update            : {counts['updated_tasks']}")
    print(f"  findings for review             : {counts['findings']}")

    changed = sorted(plan["changes"].items(),
                     key=lambda item: -len(item[1]["create"]) - len(item[1]["update"]))
    if changed:
        print("\nChanged projects")
    for project_id, change in changed[:PREVIEW_ROWS]:
        name = str(plan["tracked"][project_id].get("name"))[:44]
        print(f"  odoo#{project_id:<6} {name:<46} +{len(change['create'])} new, "
              f"{len(change['update'])} updated")
    if len(changed) > PREVIEW_ROWS:
        print(f"  ... and {len(changed) - PREVIEW_ROWS} more")


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"\n{question} [y/N]: y  (--yes)")
        return True
    if not sys.stdin.isatty():
        print(f"\n{question}\n  Not an interactive terminal, so nothing was "
              "written. Pass --yes to apply without asking.")
        return False
    while True:
        try:
            answer = input(f"\n{question} [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("  Please answer y or n.")


def step_apply(sync: ModuleType, odoo: OdooSessionClient, client: OutlineClient,
               state: State, collection_id: str, managed: dict, plan: dict,
               run_start: datetime, delay: float) -> None:
    log.info("STEP 4/4  applying the changes")
    for severity, description in plan["findings"]:
        state.record_finding(collection_id, severity, description, run_start)
    state.write()

    for project in plan["create"]:
        sync.create_tracked_project(client, odoo, state, collection_id, project,
                                    run_start, delay)

    entries = {project_id: managed[project_id] for project_id in plan["tracked"]
               if project_id in managed}
    for project_id, tree in plan["trees"].items():
        entries[project_id] = sync.adopt_tree(state, project_id, tree)

    for project_id, project in plan["tracked"].items():
        entry = entries[project_id]
        if project_id in plan["renames"]:
            sync.rename_project(client, state, entry, project)
        change = plan["changes"].get(project_id)
        if change and not entry["tasks_document_id"]:
            state.record_finding(collection_id, "error",
                                 f"No Tasks section, task changes not synced: "
                                 f"odoo#{project_id} {project.get('name')}", run_start)
            continue
        if change:
            sync.apply_change(client, odoo, state, collection_id, project, entry,
                              change, delay)
        state.mark_synced(entry["page_id"], run_start)
    state.write()


def write_run_record(run_id: str, args: argparse.Namespace, approved: bool,
                     plan: dict, counts: dict) -> None:
    record = {
        "pipeline": {
            "run_id": run_id,
            "finished_at": now_iso(),
            "practice": args.practice,
            "approved": approved,
            "counts": counts,
            "changes": {str(project_id): {
                "create": [task["id"] for task in change["create"]],
                "update": [task["id"] for task, _ in change["update"]],
            } for project_id, change in plan["changes"].items()},
        },
    }
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"pipeline_{run_id}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    log.info("pipeline run %s complete; record %s", run_id, path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Check the Odoo session, list projects on both sides, find "
                    "what changed since the last run, then ask before writing "
                    "to Atlas.")
    add_common_arguments(parser)
    parser.add_argument("--yes", action="store_true",
                        help="apply the changes without asking")
    parser.add_argument("--practice", default="Odoo",
                        help="practice to sync (default: Odoo)")
    parser.add_argument("--include-archived", action="store_true",
                        help="include archived Odoo projects and tasks")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser for the Odoo login")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between Atlas task page writes")

    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    run_id = next_run_id("pipeline")
    run_start = datetime.now(timezone.utc)
    log.info("pipeline run %s  practice=%r", run_id, args.practice)

    try:
        credentials = load_script("HQ-Credentials-Manager.py")
        scanner = load_script("HQ-Scanner.py")
        outline = load_script("Outline-Scanner.py")
        sync = load_script("HQ-Outline-Sync.py")

        step_session(credentials, open_browser=not args.no_browser)

        odoo = OdooSessionClient.from_file()
        url, key, collection_id = outline_credentials()
        client = OutlineClient(url, key)
        state = State.load()

        managed, found = step_list(scanner, outline, sync, odoo, client, state,
                                   collection_id, args)
        plan = step_changes(scanner, sync, odoo, client, state, collection_id,
                            args, managed, found)
        counts = plan_counts(plan)
        report_plan(plan, counts)

        writes = (counts["created_projects"] + counts["renamed"]
                  + counts["new_tasks"] + counts["updated_tasks"])
        approved = not writes or confirm("Apply these changes to Atlas?", args.yes)
        if approved:
            step_apply(sync, odoo, client, state, collection_id, managed, plan,
                       run_start, args.delay)
        else:
            print("\nNothing written.")
            log.info("declined at the prompt; nothing written")

        write_run_record(run_id, args, approved, plan, counts)
        return 0

    except HQError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
