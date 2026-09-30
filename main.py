from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from types import ModuleType

from hq_common import (BASE_DIR, HISTORY_DIR, HQError, OutlineClient,
                       add_common_arguments, configure_logging,
                       env_positive_int, get_logger, next_run_id, now_iso,
                       outline_credentials)

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
    log.info("STEP 1/5  checking the Odoo session")
    try:
        return credentials.ensure_session(open_browser=open_browser)
    except HQError as exc:
        raise PipelineError(f"Odoo login failed: {exc}") from None


def step_odoo_scan(scanner: ModuleType, practice: str,
                   include_archived: bool) -> dict:
    log.info("STEP 2/5  scanning Odoo")
    try:
        client = scanner.OdooSessionClient.from_file()
        return scanner.scan(client, practice=practice,
                            include_archived=include_archived)
    except HQError as exc:
        raise PipelineError(f"Odoo scan failed: {exc}") from None


def step_atlas_scan(outline: ModuleType, step: str = "STEP 3/5") -> dict:
    log.info("%s  scanning Atlas", step)
    try:
        url, key, collection = outline_credentials()
        document = outline.scan(OutlineClient(url, key), collection)
    except HQError as exc:
        raise PipelineError(f"Atlas scan failed: {exc}") from None
    return document["scan"]


def step_compare(sync: ModuleType) -> dict:
    log.info("STEP 4/5  comparing the snapshots")
    try:
        result = sync.compare(sync.load_scan(), sync.load_outline_scan(),
                              sync.load_mapping())
    except HQError as exc:
        raise PipelineError(f"Compare failed: {exc}") from None
    log.info("missing %d, needs review %d, drifted %d, atlas-only %d",
             len(result["odoo_only"]), len(result["probable"]),
             len(result["drifted"]), len(result["outline_only"]))
    return result


def step_create(sync: ModuleType, delay: float) -> int:
    log.info("STEP 5/5  creating the missing Atlas pages")
    args = argparse.Namespace(dry_run=False, limit=None, delay=delay,
                              include_probable=False, quiet=True)
    try:
        return sync.cmd_sync_all(args)
    except HQError as exc:
        raise PipelineError(f"Create failed: {exc}") from None


def sync_cap() -> int | None:
    try:
        return env_positive_int("SYNC_CAP")
    except ValueError as exc:
        raise PipelineError(str(exc)) from None


def report_missing(result: dict, cap: int | None,
                   section_count: int) -> list[dict]:
    missing = sorted(result["odoo_only"], key=lambda row: -row["odoo_tasks"])
    if not missing:
        print("\nEverything in this practice already has an Atlas page.")
        return missing

    print("\nMissing in Atlas")
    for row in missing[:PREVIEW_ROWS]:
        print(f"  odoo#{row['odoo_id']:<6} {str(row['odoo_name'])[:44]:<46} "
              f"{row['odoo_tasks']:>4} tasks")
    if len(missing) > PREVIEW_ROWS:
        print(f"  ... and {len(missing) - PREVIEW_ROWS} more")

    print(f"\n  {len(missing)} projects need to be created in Atlas.")
    if result["probable"]:
        print(f"  {len(result['probable'])} more look like duplicates of existing "
              "pages and are excluded.")

    this_run = missing if cap is None else missing[:cap]
    if cap is not None and cap < len(missing):
        print(f"  SYNC_CAP is {cap}, so this run would create {cap} of them.")
    subpages = sum(row["odoo_tasks"] for row in this_run)
    documents = len(this_run) * (1 + section_count) + subpages
    print(f"  That is {documents:,} documents: {len(this_run)} project pages, "
          f"{len(this_run) * section_count} sections, {subpages:,} task subpages.")
    return missing


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"\n{question} [y/N]: y  (--yes)")
        return True
    if not sys.stdin.isatty():
        print(f"\n{question}\n  Not an interactive terminal, so nothing was "
              "created. Pass --yes to create without asking.")
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


def write_run_record(run_id: str, args: argparse.Namespace, approved: bool,
                     odoo_result: dict, atlas_result: dict,
                     comparison: dict) -> None:
    record = {
        "pipeline": {
            "run_id": run_id,
            "finished_at": now_iso(),
            "practice": args.practice,
            "approved": approved,
            "skipped_scan": args.skip_scan,
            "odoo_scan": odoo_result or None,
            "atlas_scan": atlas_result or None,
            "counts": {
                "to_create": len(comparison["odoo_only"]),
                "needs_review": len(comparison["probable"]),
                "drifted": len(comparison["drifted"]),
                "atlas_only": len(comparison["outline_only"]),
                "linked": len(comparison["linked"]),
            },
        },
    }
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"pipeline_{run_id}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    log.info("pipeline run %s complete; record %s", run_id, path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Check the Odoo session, scan Odoo and Atlas, compare, then "
                    "ask before creating the missing Atlas pages.")
    add_common_arguments(parser)
    parser.add_argument("--yes", action="store_true",
                        help="create the missing pages without asking")
    parser.add_argument("--practice", default="Odoo",
                        help="practice to scan (default: Odoo)")
    parser.add_argument("--include-archived", action="store_true",
                        help="include archived Odoo projects and tasks")
    parser.add_argument("--skip-scan", action="store_true",
                        help="reuse the snapshots already on disk")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser for the Odoo login")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between Atlas document creates")

    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    run_id = next_run_id("pipeline")
    log.info("pipeline run %s  practice=%r", run_id, args.practice)

    try:
        credentials = load_script("HQ-Credentials-Manager.py")
        scanner = load_script("HQ-Scanner.py")
        outline = load_script("Outline-Scanner.py")
        sync = load_script("HQ-Outline-Sync.py")

        step_session(credentials, open_browser=not args.no_browser)

        odoo_result: dict = {}
        atlas_result: dict = {}
        if args.skip_scan:
            log.info("STEP 2-3/5  skipped, reusing the snapshots on disk")
        else:
            odoo_result = step_odoo_scan(scanner, args.practice,
                                         args.include_archived)
            atlas_result = step_atlas_scan(outline)

        comparison = step_compare(sync)
        missing = report_missing(comparison, sync_cap(), len(sync.SECTIONS))

        exit_code = 0
        approved = False
        if missing:
            approved = confirm("Create these projects in Atlas?", args.yes)
            if approved:
                exit_code = step_create(sync, args.delay)
                log.info("refreshing the Atlas snapshot after writing")
                atlas_result = step_atlas_scan(outline, step="STEP 5/5")
            else:
                print("\nNothing created.")
                log.info("declined at the prompt; nothing written to Atlas")

        write_run_record(run_id, args, approved, odoo_result, atlas_result,
                         comparison)
        return exit_code

    except HQError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
