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


def step_session(credentials: ModuleType, open_browser: bool,
                 allow_login: bool = True) -> dict:
    log.info("Checking the Odoo sign-in...")
    try:
        return credentials.ensure_session(open_browser=open_browser,
                                          allow_login=allow_login)
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
    log.info("Loading Odoo projects and Atlas pages...")
    collection = client.collection_info(collection_id)
    state.ensure_collection(collection_id, collection["name"],
                            collection["createdAt"], sync.SECTIONS)
    projects = scanner.list_projects(odoo, args.practice, args.include_archived)
    roots = outline.list_roots(client, collection_id)
    managed = state.managed_projects()
    found = sync.reconcile(projects, roots, managed)
    log.info("Found %d Odoo projects and %d Atlas pages. %d are already tracked; "
             "%d existing pages need checking; %d projects may need new pages.",
             len(projects), len(roots), len(found["managed"]),
             len(found["adopt"]), len(found["create"]))
    return managed, found


def step_changes(scanner: ModuleType, sync: ModuleType, odoo: OdooSessionClient,
                 client: OutlineClient, state: State, collection_id: str,
                 args: argparse.Namespace, managed: dict, found: dict) -> dict:
    log.info("Checking which pages and tasks have changed...")
    cap = sync_cap()
    create = found["create"] if cap is None else found["create"][:cap]
    tasks_of: dict[int, list[dict]] = {}
    if create:
        log.info("Loading tasks for %d new projects...", len(create))
    for task in scanner.fetch_tasks(odoo, [project["id"] for project in create],
                                    args.include_archived):
        tasks_of.setdefault(task["project_id"], []).append(task)
    for project in create:
        project["tasks"] = tasks_of.get(project["id"], [])

    trees = {}
    to_check = len(found["adopt"])
    if to_check:
        log.info("Reading %d existing Atlas projects. Progress will appear "
                 "as each group is checked.", to_check)
    task_pages_found = 0
    for number, (project, root) in enumerate(found["adopt"], start=1):
        tree = sync.read_tree(client, collection_id, root)
        trees[project["id"]] = tree
        task_pages_found += len(tree["tasks"])
        if number == 1 or number % 10 == 0 or number == to_check:
            log.info("Checked %d/%d existing Atlas projects (%d task pages found).",
                     number, to_check, task_pages_found)
    tracked = {project["id"]: project for project in found["managed"]}
    tracked.update({project["id"]: project for project, _ in found["adopt"]})

    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    since = {project_id: managed[project_id]["watermark"] - skew
             for project_id in tracked
             if project_id in managed and managed[project_id]["watermark"]}
    oldest = min(since.values(), default=None)
    log.info("Looking for changed Odoo tasks in %d tracked projects...",
             len(tracked))
    tasks = scanner.fetch_tasks(odoo, list(since), args.include_archived,
                                oldest.strftime(sync.ODOO_TIME) if oldest else None)
    tasks += scanner.fetch_tasks(odoo, [project_id for project_id in tracked
                                        if project_id not in since],
                                 args.include_archived)
    log.info("Loaded %d Odoo tasks to compare with Atlas.", len(tasks))

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
    print("\nWhat this run found")
    print(f"  New projects: {counts['created_projects']} of {plan['available']} "
          f"to create, with {counts['created_project_tasks']} task pages")
    if plan["cap"] is not None and plan["cap"] < plan["available"]:
        print(f"  Creation is limited to {plan['cap']} projects by SYNC_CAP.")
    print(f"  Existing Atlas projects to track: {counts['adopted']}")
    print(f"  Project names to update: {counts['renamed']}")
    print(f"  New task pages: {counts['new_tasks']}")
    print(f"  Task pages to update: {counts['updated_tasks']}")
    print(f"  Items needing review: {counts['findings']}")

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
    log.info("Saving the sync results...")
    for severity, description in plan["findings"]:
        state.record_finding(collection_id, severity, description, run_start)
    state.write()

    for number, project in enumerate(plan["create"], start=1):
        log.info("Creating project %d/%d: %s (%d tasks).", number,
                 len(plan["create"]), project.get("name") or project["id"],
                 len(project["tasks"]))
        sync.create_tracked_project(client, odoo, state, collection_id, project,
                                    run_start, delay)

    entries = {project_id: managed[project_id] for project_id in plan["tracked"]
               if project_id in managed}
    for number, (project_id, tree) in enumerate(plan["trees"].items(), start=1):
        entries[project_id] = sync.adopt_tree(state, project_id, tree)
        if number == 1 or number % 10 == 0 or number == len(plan["trees"]):
            log.info("Saved %d/%d existing Atlas projects to sync state.",
                     number, len(plan["trees"]))

    for project_id, project in plan["tracked"].items():
        entry = entries[project_id]
        if project_id in plan["renames"]:
            sync.rename_project(client, state, entry, project)
        change = plan["changes"].get(project_id)
        if change and not entry["tasks_document_id"]:
            log.warning("Project %s has no Tasks section; its %d task changes "
                        "were skipped.", project_id,
                        len(change["create"]) + len(change["update"]))
            state.record_finding(collection_id, "error",
                                 f"No Tasks section, task changes not synced: "
                                 f"odoo#{project_id} {project.get('name')}", run_start)
            continue
        if change:
            log.info("Syncing tasks for %s: %d new, %d changed.",
                     project.get("name") or project_id, len(change["create"]),
                     len(change["update"]))
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
            "unattended": args.unattended,
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
    log.info("Sync finished. Run details saved to %s.", path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Check the Odoo session, list projects on both sides, find "
                    "what changed since the last run, then sync to Atlas "
                    "after approval or in unattended mode.")
    add_common_arguments(parser)
    parser.add_argument("--yes", action="store_true",
                        help="apply the changes without asking")
    parser.add_argument("--unattended", action="store_true",
                        help="run without prompts; apply changes and fail if "
                             "the Odoo session needs a new login")
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
    log.info("Starting sync for practice %s (run %s).",
             args.practice or "all customer practices", run_id)

    try:
        credentials = load_script("HQ-Credentials-Manager.py")
        scanner = load_script("HQ-Scanner.py")
        outline = load_script("Outline-Scanner.py")
        sync = load_script("HQ-Outline-Sync.py")

        step_session(credentials, open_browser=not args.no_browser,
                     allow_login=not args.unattended)

        odoo = OdooSessionClient.from_file()
        url, key, collection_id = outline_credentials()
        client = OutlineClient(url, key)
        state = State.load()

        managed, found = step_list(scanner, outline, sync, odoo, client, state,
                                   collection_id, args)
        plan = step_changes(scanner, sync, odoo, client, state, collection_id,
                            args, managed, found)
        counts = plan_counts(plan)
        if args.unattended:
            log.info("Plan: %d new projects (%d task pages), %d existing "
                     "projects to track, %d renames, %d new tasks, %d task "
                     "updates, %d items for review.",
                     counts["created_projects"], counts["created_project_tasks"],
                     counts["adopted"], counts["renamed"], counts["new_tasks"],
                     counts["updated_tasks"], counts["findings"])
        else:
            report_plan(plan, counts)

        writes = (counts["created_projects"] + counts["renamed"]
                  + counts["new_tasks"] + counts["updated_tasks"])
        approved = not writes or confirm("Apply these changes to Atlas?",
                                        args.yes or args.unattended)
        if approved:
            step_apply(sync, odoo, client, state, collection_id, managed, plan,
                       run_start, args.delay)
        else:
            print("\nNothing written.")
            log.info("Changes were declined; Atlas was not updated.")

        write_run_record(run_id, args, approved, plan, counts)
        return 0

    except HQError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Sync stopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
