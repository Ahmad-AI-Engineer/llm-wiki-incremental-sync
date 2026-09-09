# HQ Odoo → Outline sync pipeline

Syncs Odoo project data into the **Atlas** Outline knowledge base
(`https://atlas.axm.app`, collection *Odoo Projects*).

Source: Odoo 19 at `https://theaxiomworld.com`, db `Axiom_HQ_19`.
No dependencies — Python 3.11+ standard library only.

## Running it

```bash
python HQ-Credentials-Manager.py login   # once, until the session expires
python main.py                           # dry run: scan both sides, compare, plan
python main.py --apply                   # same, then create pages (honours SYNC_CAP)
```

`main.py` is the entry point and runs all four steps in one process:

```
STEP 1/4  scan Odoo     -> output/projects_and_tasks.json
STEP 2/4  scan Outline  -> output/outline_projects.json
STEP 3/4  compare snapshots
STEP 4/4  create the missing Outline pages   (--apply only)
```

With `--apply` it re-scans Outline afterwards, so the snapshot reflects what it
just created. Useful flags: `--skip-scan` (reuse snapshots on disk),
`--practice X`, `--include-archived`, `--delay`, `-v`.

Each run writes `output/history/pipeline_<run_id>.json` recording the mode, the
scan run ids it used, and the resulting counts.

The individual scripts below remain runnable on their own for inspection and
for one-off work.

## The scripts

| Script | Role |
| --- | --- |
| `main.py` | **entry point** — runs the whole pipeline |
| `HQ-Credentials-Manager.py` | Odoo username/password → session token |
| `HQ-Scanner.py` | Odoo → `output/projects_and_tasks.json` |
| `Outline-Scanner.py` | Outline → `output/outline_projects.json` |
| `HQ-Outline-Sync.py` | compare the two snapshots, create Outline pages |
| `hq_common.py` | shared env loading, run ids, logging |

Both sides produce a snapshot on disk, so comparison is reproducible and needs
no API access.

`hq_common.py` exists because the four scripts have hyphens in their names,
which are not valid Python module names — so they cannot import each other.
It holds the logic that would otherwise be copied four times.

## Run ids, file naming and logging

Every scan and sync gets a sequential per-day id, so runs never mix:

```
output/history/odoo_20260909-001.json
output/history/outline_20260909-001.json
output/history/sync_20260909-001.json
```

The counter is derived from the files already in `output/history/`, so ids
continue correctly across restarts. Each sync record names the exact Odoo and
Outline run ids it compared.

Logging uses the standard `logging` module: console at INFO (`-v` for DEBUG,
or `LOG_LEVEL` in `.env`) and a full DEBUG trail in
`output/logs/pipeline.log`, rotating at 5 MB with 3 backups. Reports go to
stdout; logs go to stderr, so `command > report.txt` keeps them separate.

## The sync cap

`SYNC_CAP` in `.env` limits how many projects one `sync-all` run creates:

```
SYNC_CAP=4        # create at most 4 projects per run
SYNC_CAP=         # empty or absent: no cap, create all of them
```

`--limit N` on the command line overrides it, `--limit 0` creates nothing, and
a non-numeric or zero/negative `SYNC_CAP` fails loudly rather than silently
disabling the cap.

## 1. Authenticate

```bash
python HQ-Credentials-Manager.py ui       # browser form at 127.0.0.1:8765
python HQ-Credentials-Manager.py login    # terminal, hidden password
python HQ-Credentials-Manager.py status   # validate the stored token
python HQ-Credentials-Manager.py logout
```

Odoo issues no OAuth token for username/password login — it issues a
`session_id` cookie, and that is what gets stored in
`credentials/odoo_session.json` (gitignored). The password is used once and
never stored.

Login posts the HTML form at `/web/login` with its CSRF token, which means **the
database name is never needed** — the server resolves it via dbfilter and
`get_session_info` reports it back. A JSON-RPC `/web/session/authenticate` path
exists as a fallback if a db name is supplied. Accounts with two-factor auth
cannot complete this flow and the error says so.

## 2. Scan Odoo

```bash
python HQ-Scanner.py practices             # practices + project counts
python HQ-Scanner.py practice-of 1068      # practice of one project
python HQ-Scanner.py scan --practice Odoo  # the scan (Odoo is the default)
```

### How practice is determined

`project.project` has no practice column. The practice is the **product
category of the sale order line that created the project**:

```
project_project.sale_line_id  →  sale_order_line.id
sale_order_line.categ_id      →  product_category.name
```

Many-to-one, and the FK is nullable. `product.category` is a curated tree that
is exactly the practice list, so practice is read from a real field:

| Practice | Projects |
| --- | --- |
| **Odoo** | 311 |
| Accounting | 79 |
| Analytics | 57 |
| Website/Design | 32 |
| DevOps | 19 |
| NetSuite | 15 |
| (no sale order — internal projects) | 209 |

Odoo writes `sale_line_id` when a service product with *Service Tracking =
create a project* is sold. Projects created by hand never get it, so internal
work has no determinable practice — scoped out for now by decision.

Rejected alternatives, by measurement: no "ERP" category exists; `hr.department`
resolves 0 Odoo projects (the Odoo department has no employees); `tag_ids`
covers 228 of 696 projects; `x_plan2_id`/`x_plan3_id` are empty.

### Scan output

Project name, practice, task names and assignees:

```json
{
  "id": 992, "name": "S00667 - America Cryo", "practice": "Odoo",
  "practice_source": "product_category",
  "practice_evidence": "Odoo Services - Prepaid/Fixed Price",
  "tasks": [{ "id": 1, "name": "Sale Order Discovery Session",
              "assignees": ["Muhammad usman Talib", "Sameer Ahmad"] }]
}
```

Current: **310 projects, 12,189 tasks, ~1.3 MB.**

## 3. Scan Outline

```bash
python Outline-Scanner.py scan       # snapshot the collection
python Outline-Scanner.py sections   # section-title vocabulary
```

Current: **125 project pages, 2,439 documents.**

A project in Outline is a top-level document; each section is a **child
document**, and tasks are often grandchildren. The scanner rebuilds that tree
locally from one paginated `documents.list` call and extracts task names both
from markdown tables and from child document titles.

Section naming across the 125 existing pages is inconsistent — 148 distinct
section titles, 122 of them used only once:

```
53 Overview   51 Credentials   39 Tasks   38 Meetings   31 Task
30 Scope of Work   28 Meeting   14 JD   13 Project Plan   13 Proposal
```

## 4. Compare and sync

```bash
python HQ-Outline-Sync.py status                  # connection, snapshots, cap
python HQ-Outline-Sync.py compare --show all      # offline, both snapshots
python HQ-Outline-Sync.py candidates              # safe to create
python HQ-Outline-Sync.py sync 992                # DRY RUN, one project
python HQ-Outline-Sync.py sync 992 --apply        # writes to Outline
python HQ-Outline-Sync.py sync-all                # DRY RUN, everything missing
python HQ-Outline-Sync.py sync-all --apply        # honours SYNC_CAP
```

`sync-all` saves the mapping after **every** project, so an interrupted run is
resumable — already-created projects are matched by their id marker on the next
run and skipped. Failures are logged and do not abort the batch, and HTTP 429
is retried with backoff.

### Identity matching

The two systems share no key. Outline titles are client-friendly
(`Stratus Plastics - Colby`); Odoo names are sale-order prefixed
(`S00216 - Stratus Plastics (Colby)`). Matching is tried in this order:

1. **`<!-- odoo-project-id: N -->` marker** written into pages this pipeline creates
2. **`output/outline_mapping.json`** — Odoo id → Outline document ids
3. **normalized title** — strips the `S00xxx -` prefix and punctuation
4. **token similarity** — never auto-matched, only reported for review

Step 4 matters: exact matching missed `S00636 - Peter and June` ↔
`Peter & June` (similarity 1.00) and `Jon Parrish` ↔ `Jon Parish`. `sync`
refuses to create when similarity ≥ 0.34 unless `--force`, so create-only
cannot silently duplicate an existing page.

### Page template

Seven child documents per project, created in this order:

```
Overview · Customer Introduction · Tasks · Roadmap · Customizations ·
Scope of Work · Meetings
```

Only **Tasks** has an Odoo source (a task/assignee table). The other six are
created empty for humans to fill. Outline inserts new siblings at the top, so
sections are created in reverse to land in the intended order.

### Safety

- `sync` is **dry-run by default**; `--apply` is required to write.
- **Create-only** — it never edits or renames the 125 existing pages.
- Refuses to act on a project already in the mapping, or one with a
  similarly-titled page.
- Every write is appended to `output/sync_log.md`.
- The Outline API key is an **admin** key on a live knowledge base.

## Current sync state

```
linked by id marker/mapping :    1
matched by title only       :   22
probable match, needs review:   43
in Odoo only  (to create)   :  244
in Outline only (orphans)   :  102
matched but drifted         :   22
```

All 22 title-matched pages have drifted: Odoo consistently holds far more tasks
than Outline lists (`Askari GGS` 236 vs 10, `Stratus Plastics` 275 vs 33), and
most are missing several template sections. The 102 "orphans" include the pages
behind the 43 probable matches, so they are not all unrelated.

## Output files

```
credentials/odoo_session.json         Odoo session token (gitignored)
output/projects_and_tasks.json        latest Odoo scan
output/outline_projects.json          latest Outline snapshot
output/outline_mapping.json           Odoo id → Outline document ids
output/sync_log.md                     append-only record of every write
output/history/<name>_<run_id>.json    every scan of both sides, kept
```

Nothing prunes `output/history/`.

## Not built yet

- **Update path.** Sync is create-only, so the 22 drifted pages stay stale.
- **The AI layer.** Tasks are copied mechanically. `Overview`,
  `Customer Introduction` and the rest need synthesis; the one good example
  (`Yunak LLC - Alper`) was researched from the client's website, which Odoo
  cannot supply.
- **Change-driven ingest.** Snapshots are kept but not diffed run-to-run, so
  there is no "what changed since last sync" changeset yet.
- **Scheduling.** Everything is run by hand.
