# HQ Odoo → Outline sync pipeline

Syncs Odoo project data into the **Atlas** Outline knowledge base
(`https://atlas.axm.app`, collection *Odoo Projects*).

Source: Odoo 19 at `https://theaxiomworld.com`, db `Axiom_HQ_19`.
No dependencies — Python 3.11+ standard library only.

## Running it

```bash
python main.py
```

That is the whole thing. It runs five steps in one process:

```
STEP 1/5  check the Odoo session   (opens the browser to log in if there is none)
STEP 2/5  scan Odoo     -> output/projects_and_tasks.json
STEP 3/5  scan Atlas    -> output/outline_projects.json
STEP 4/5  compare the snapshots
STEP 5/5  create the missing Atlas pages   (only after you answer yes)
```

Step 1 validates the stored `session_id` against Odoo. If the file is missing,
empty or the session has expired, it serves the login page at
`127.0.0.1:8765`, opens a browser, and waits. On success it writes
`credentials/odoo_session.json` and carries on — no restart needed.

Step 4 prints the first few projects that have no Atlas page, then the total,
then asks:

```
Missing in Atlas
  odoo#377    S00258 - 12panel Upgrdation V17                 229 tasks
  odoo#205    S00111 - The Seating Shoppe, Luzy Stein         221 tasks
  ... and 231 more

  236 projects need to be created in Atlas.
  47 more look like duplicates of existing pages and are excluded.
  SYNC_CAP is 4, so this run would create 4 of them.

Create these projects in Atlas? [y/N]:
```

Answering `n` (or just Enter) terminates without writing anything. Answering
`y` creates the pages and re-scans Atlas so the snapshot reflects them.

Nothing reaches Atlas without a `y`. Piped or scheduled runs are not
interactive, so they refuse to create unless you pass `--yes`.

Useful flags: `--yes` (skip the prompt), `--skip-scan` (reuse the snapshots on
disk), `--no-browser`, `--practice X`, `--include-archived`, `--delay`, `-v`.

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
| `hq_common.py` | shared paths, env loading, run ids, logging, Outline client |
| `ui/` | the browser login page (`login.html`, `login.css`) |

Both sides produce a snapshot on disk, so comparison is reproducible and needs
no API access.

`hq_common.py` exists because the four scripts have hyphens in their names,
which are not valid Python module names — so they cannot import each other.
It holds the logic that would otherwise be copied four times: the output paths,
the `HQError` base class every script reports through, `.env` loading, run ids,
logging, and the `OutlineClient`.

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
or `LOG_LEVEL` in `.env`; `-v` wins) and a full DEBUG trail in
`output/logs/pipeline.log`, rotating at 5 MB with 3 backups. Reports go to
stdout; logs go to stderr, so `command > report.txt` keeps them separate.

Console output is forced to UTF-8. Some project names carry characters a
Windows cp1252 console cannot encode, which otherwise aborts a report
part-way through.

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

`main.py` does this for you when it needs to. These are for doing it by hand:

```bash
python HQ-Credentials-Manager.py ui       # browser form at 127.0.0.1:8765
python HQ-Credentials-Manager.py login    # terminal, hidden password
python HQ-Credentials-Manager.py status   # validate the stored token
python HQ-Credentials-Manager.py logout
```

The login page asks only for a username and password. The server it signs in
to is fixed by `ODOO_URL` in `.env` and shown on the page, so a run can never
point at the wrong Odoo by mistake. Its markup lives in `ui/`, not in the
Python.

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
