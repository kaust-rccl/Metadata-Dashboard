# Recovering a corrupt tracker database

## Symptoms

- A listing shows the same artifact twice; Streamlit raises
  `StreamlitDuplicateElementKey` (older builds).
- A full scan returns a row twice while a lookup by rowid finds it once:
  ```sql
  SELECT _rowid_, artifact_name FROM artifacts WHERE artifact_name LIKE 'X%';  -- 2 rows
  SELECT _rowid_, artifact_name FROM artifacts WHERE _rowid_ = 27;              -- 1 row
  ```
- The dashboard sidebar shows **"The tracker database is corrupt"**, and sync
  reports that it made no changes.

This is page-level corruption of the SQLite file (a B-tree page referenced
twice). Rows on a page that became unreachable are missing from listings too.
Nothing in the app can repair it in place; rebuild the file as below.

## Procedure

Run from the repo root. `python3 -m sqlite3` takes SQL only — no dot-commands.

1. **Stop every process that opens the DB, on every login node** — the dashboard,
   the CLI, and any sysadmin tooling:
   ```bash
   pgrep -u $USER -af "streamlit|cli.py"
   ```
2. **Confirm** (and check `transfers.db` the same way):
   ```bash
   python3 -m sqlite3 /scratch/project/k03/support_team/mock_db/tracker.db
   sqlite> PRAGMA integrity_check;
   ```
3. **Back up the corrupt file**, including any `-journal`, `-wal` or `-shm`
   file next to it:
   ```bash
   cd /scratch/project/k03/support_team/mock_db
   for f in tracker.db*; do cp -p "$f" "$f.corrupt-$(date +%F)"; done
   ```
4. **Rebuild** — read-only on the source, writes a new file:
   ```bash
   python3 tools/recover_db.py tracker.db tracker.recovered.db --dry-run
   python3 tools/recover_db.py tracker.db tracker.recovered.db
   ```
   Check the report: per table, `rows` is what the new file holds,
   `dup_dropped`/`double_refs` show the corruption, `from_orphans` counts
   rows recovered from unreachable pages. It must end `integrity_check: ok`.
   Rows whose table could not be determined are in a `lost_and_found` table
   of the new file — review them; they are never mixed into real tables.
5. **Swap**, with everything still stopped:
   ```bash
   mv tracker.db tracker.db.replaced-$(date +%F)
   mv tracker.recovered.db tracker.db
   chgrp k03 tracker.db && chmod g+rw tracker.db
   ```
6. **Start the dashboard once** (it adds the unique index on `yml_path`), then:
   ```bash
   dashboard sync --dry-run    # review
   dashboard sync
   ```
   Sync restores any artifact that has a YML but is missing from the DB.

## Prevention

SQLite is only safe on Lustre when file locks work **across nodes**:

```bash
mount -t lustre | grep -oE '\b(flock|localflock|noflock)\b' | sort -u
```

- `flock` — cluster-wide locks; safe with DELETE journal mode (the app's default).
- `localflock` — locks are visible **only on the node that took them**. Two
  writers on different login nodes (dashboard on login2, CLI on login1, the
  sysadmin tool elsewhere) can write at the same time and corrupt the file.
  Run every writer from a single node, or move the DB off Lustre.
- `noflock` — no locking at all; do not keep a shared SQLite DB there.

WAL mode needs shared memory between processes, which Lustre does not
provide across nodes — the app now switches the DB to DELETE mode. Never
copy the DB file while anything has it open.
