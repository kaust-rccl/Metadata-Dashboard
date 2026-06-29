# HPC Transfer Pipeline — User Guide

**DART · Shaheen3 HPC · KAUST · k03**  
*For system administrators handling ticket-driven data transfers*

---

## Overview

The HPC pipeline handles data movement requests that arrive via the RT ticketing system and have no AIMCR proposal lineage. You receive a ticket, move data from one location to another on Shaheen3, verify it arrived intact, and log the outcome — all tracked in DART.

Unlike the HPC-AI pipeline there are no YAML metadata files written to the source or destination. All records live in the database, and a workspace directory is created per transfer to hold the generated Slurm scripts, logs, and verification result.

**What DART does in this pipeline:**
- Creates a database record for the transfer
- Generates the checksum and verification Slurm scripts for you
- Parses the verification result and marks the transfer complete or anomaly
- Maintains a full audit trail of every action

**What you do outside DART:**
- Submit the generated Slurm scripts
- Run the actual data transfer (`rsync`)

---

## Navigating to the HPC pipeline

In the sidebar, under **Data movement pipeline**, select **HPC**. This shows a single page — **HPC Transfer** — with four tabs: New transfer, Pre-move, Post-move, and Transfer log.

---

## Tab 1 — New transfer

Open this tab when a new RT ticket comes in.

Fill in all required fields:

| Field | Description |
|---|---|
| RT ticket number | The RT ticket number for this request |
| Artifact name | A short identifier for what is being moved (e.g. `imagenet-val`) |
| Requester username | The Shaheen3 username of the person who raised the ticket |
| Type | `dataset`, `model`, `software`, or `other` |
| Source path | Full path to the artifact on the source filesystem (cpup) |
| Destination path | Full path to the destination (gpup) |
| Size (GB) | Approximate size — used for record-keeping only |
| Notes | Any context about the transfer |

Click **Create transfer record**. A database record is created and a workspace directory is provisioned at:

```
/scratch/project/k03/support_team/workspace/<ticket>_<name>/
```

Note the transfer ID shown in the success message — it is used throughout the rest of the process.

---

## Tab 2 — Pre-move

Open this tab after creating the transfer record, before moving any data.

**Purpose:** Generate a checksum of all files at the source. This is the reference fingerprint used to verify the transfer later.

**Steps:**

1. Select the transfer from the dropdown — ticket, artifact name, and source path are filled automatically.
2. The dashboard shows where the checksum file will be written: inside the source directory (or next to the file if it is a single file).
3. Click **Generate & save pre-move script**. The dashboard writes `generate_checksum.slurm` to the workspace.
4. Submit the script yourself:

```bash
sbatch /scratch/project/k03/support_team/workspace/<ticket>_<name>/generate_checksum.slurm
```

5. Monitor the job:

```bash
squeue --me
tail -f /scratch/project/k03/support_team/workspace/<ticket>_<name>/logs/checksum_generate_<JOBID>.log
```

Once the job completes, `checksum.sha256` is written inside the source directory. The checksum uses relative paths so it remains valid wherever the data is moved to.

**Then move the data:**

```bash
rsync -avP /source/path/ /destination/path/
```

The checksum file is inside the source directory and travels with the data automatically.

---

## Tab 3 — Post-move

Open this tab after the data has been transferred to the destination.

This tab has two steps.

### Step 1 — Generate verification script

1. Select the same transfer from the dropdown.
2. Click **Generate & save verify script**. The dashboard writes `verify_checksum.slurm` to the workspace.
3. Submit at the destination:

```bash
sbatch /scratch/project/k03/support_team/workspace/<ticket>_<name>/verify_checksum.slurm
```

4. Monitor:

```bash
tail -f /scratch/project/k03/support_team/workspace/<ticket>_<name>/logs/checksum_verify_<JOBID>.log
```

The script verifies every file, counts passes and failures, and writes `verify_result.txt` to the workspace.

### Step 2 — Import verification result

Once the verification job completes:

1. The Transfer ID and result file path (`workspace/<ticket>_<name>/verify_result.txt`) are pre-filled.
2. Click **Import result & complete record**.
3. DART parses the result file, records the pass/fail counts, and marks the transfer:
   - All files OK → **completed**
   - Any failures → **anomaly**

---

## Tab 4 — Transfer log

Shows all transfers with their current status. Use the search box to filter by ticket number and the status dropdown to filter by stage.

Click **Detail** on any row to open the detail view, which shows:

- Full transfer metadata
- Verification counts (if verification has been run)
- Modification log — every action with actor, timestamp, and note
- Audit log (DB) — the database-level event log
- **Edit record** — all fields are editable. If you edit a field that affects an already-generated script (source path, destination path, ticket number, or artifact name), a sync warning appears listing which scripts need to be regenerated.
- **Update status** — manually set the status with an optional note
- **Delete** — removes the record from the database

---

## Status lifecycle

| Status | Set by | Meaning |
|---|---|---|
| `pending` | New transfer tab | Record created |
| `pre-move` | Pre-move tab | Checksum script generated |
| `in-progress` | Manual update | Transfer underway |
| `verifying` | Post-move tab | Verify script generated |
| `completed` | Post-move tab | All files verified OK |
| `anomaly` | Post-move tab | One or more files failed verification |
| `blocked` | Manual update | Transfer on hold |

---

## Workspace layout

Every transfer has a dedicated workspace directory:

```
/scratch/project/k03/support_team/workspace/<ticket>_<name>/
├── generate_checksum.slurm     generated by Pre-move tab
├── verify_checksum.slurm       generated by Post-move tab
├── verify_result.txt           written by verify job
├── artifact.yml                metadata record, updated at each stage
├── artifact.modlog.yml         append-only, one entry per event
└── logs/
    ├── checksum_generate_<jobid>.log
    └── checksum_verify_<jobid>.log
```

---

## Handling anomalies

If verification finds failed files the transfer is marked `anomaly`. Do not close the RT ticket yet.

1. Open `verify_result.txt` in the workspace — lines ending in `FAILED` list the affected files.
2. Compare sizes at source and destination:
   ```bash
   ls -lh /source/path/file /destination/path/file
   ```
3. Re-rsync the failed files:
   ```bash
   rsync -avP /source/path/failed_file /destination/path/
   ```
4. Re-generate and re-submit the verify script from the Post-move tab.
5. Once all files pass, the transfer is marked `completed`. Close the RT ticket.

---

## Editing a record after scripts are generated

If you need to correct a field after a script has already been written:

1. Open the transfer in the **Transfer log** detail view.
2. Edit the field in the **Edit record** section and click **Save changes**.
3. If the changed field affects a generated script, a warning appears:

   > ⚠️ Scripts out of sync — `generate_checksum.slurm` affected by `source_path`

4. Go back to the Pre-move or Post-move tab, select the transfer, and regenerate the affected script. The old script in the workspace is overwritten.

---

*DART · HPC Transfer Pipeline · Shaheen3 · KAUST · k03*