from __future__ import annotations
import argparse
import importlib.util
import json
from types import ModuleType

from hq_common import (BASE_DIR, HISTORY_DIR, add_common_arguments, next_run_id,
                       now_iso, setup_logging)

log = setup_logging("pipeline")


class PipelineError(RuntimeError):
    pass


def load_script(filename: str) -> ModuleType:
    """Import a hyphenated script file as a module."""
    path = BASE_DIR / filename
    if not path.exists():
        raise PipelineError(f"Missing script: {path}")
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def step_odoo_scan(scanner: ModuleType, practice: str,
                   include_archived: bool) -> dict:
    log.info("STEP 1/4  scanning Odoo")
    try:
        client = scanner.OdooSessionClient.from_file()
    except scanner.ScanError as exc:
        raise PipelineError(f"{exc}") from None
    try:
        return scanner.scan(client, practice=practice,
                            include_archived=include_archived)
    except scanner.ScanError as exc:
        raise PipelineError(f"Odoo scan failed: {exc}") from None


def step_outline_scan(outline: ModuleType) -> dict:
    log.info("STEP 2/4  scanning Outline")
    try:
        url, key, collection = outline.outline_credentials()
        document = outline.scan(outline.OutlineClient(url, key), collection)
    except outline.OutlineScanError as exc:
        raise PipelineError(f"Outline scan failed: {exc}") from None
    return document["scan"]


def step_compare(sync: ModuleType) -> dict:
    log.info("STEP 3/4  comparing snapshots")
    try:
        result = sync.compare(sync.load_scan(), sync.load_outline_scan(),
                              sync.load_mapping())
    except sync.SyncError as exc:
        raise PipelineError(f"Compare failed: {exc}") from None
    log.info("to create %d, needs review %d, drifted %d, outline-only %d",
             len(result["odoo_only"]), len(result["probable"]),
             len(result["drifted"]), len(result["outline_only"]))
    return result


def step_sync(sync: ModuleType, apply: bool, delay: float) -> int:
    log.info("STEP 4/4  %s Outline pages", "creating" if apply else "planning")
    args = argparse.Namespace(dry_run=not apply, limit=None, delay=delay,
                              include_probable=False)
    try:
        return sync.cmd_sync_all(args)
    except sync.SyncError as exc:
        raise PipelineError(f"Sync failed: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Run the whole pipeline: scan Odoo, scan Outline, compare, "
                    "then create the missing Outline pages (capped by SYNC_CAP).")
    add_common_arguments(parser)
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only, write nothing to Outline")
    parser.add_argument("--practice", default="Odoo",
                        help="practice to scan (default: Odoo)")
    parser.add_argument("--include-archived", action="store_true",
                        help="include archived Odoo projects and tasks")
    parser.add_argument("--skip-scan", action="store_true",
                        help="reuse the snapshots already on disk")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between Outline document creates")

    args = parser.parse_args(argv)
    if args.verbose:
        setup_logging("pipeline", verbose=True)

    apply = not args.dry_run
    run_id = next_run_id("pipeline")
    log.info("pipeline run %s  practice=%r  mode=%s", run_id, args.practice,
             "writing to Outline" if apply else "dry run")

    try:
        scanner = load_script("HQ-Scanner.py")
        outline = load_script("Outline-Scanner.py")
        sync = load_script("HQ-Outline-Sync.py")

        odoo_result: dict = {}
        outline_result: dict = {}
        if args.skip_scan:
            log.info("STEP 1-2/4  skipped, reusing snapshots on disk")
        else:
            odoo_result = step_odoo_scan(scanner, args.practice,
                                         args.include_archived)
            outline_result = step_outline_scan(outline)

        comparison = step_compare(sync)
        exit_code = step_sync(sync, apply, args.delay)

        if apply:
            log.info("refreshing the Outline snapshot after writing")
            outline_result = step_outline_scan(outline)

        record = {
            "pipeline": {
                "run_id": run_id,
                "finished_at": now_iso(),
                "practice": args.practice,
                "applied": apply,
                "skipped_scan": args.skip_scan,
                "odoo_scan": odoo_result or None,
                "outline_scan": outline_result or None,
                "counts": {
                    "to_create": len(comparison["odoo_only"]),
                    "needs_review": len(comparison["probable"]),
                    "drifted": len(comparison["drifted"]),
                    "outline_only": len(comparison["outline_only"]),
                    "linked": len(comparison["linked"]),
                },
            },
        }
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = HISTORY_DIR / f"pipeline_{run_id}.json"
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        log.info("pipeline run %s complete; record %s", run_id, path.name)
        return exit_code

    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
