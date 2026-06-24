# Artifact Curation & Movement Guide

**Shaheen3 HPC · KAUST · k03**

How to use the Metadata Dashboard across the full lifecycle — from Ibex download to gpup destination.

---

## End-to-end flow

```
Source → Ibex → sh3 cpup → Dashboard → sh3 gpup → Available
```

> **The dashboard sits between staging and movement.** It does not move data — it tracks, validates, and creates an auditable metadata record of every artifact as it moves through the system. The actual file transfers happen outside the dashboard using standard HPC tools.

---

## Curator — Support Team

*Downloads, verifies, and stages artifacts. Creates the metadata record. Opens RT tickets.*

### Phase 1 — Ibex

**Step 1 — Download the artifact on Ibex**

Log into Ibex and download the artifact from the source — a third-party provider, PI storage, or external URL. Keep the original directory structure intact.

```bash
# on Ibex
ssh username@glogin.ibex.kaust.edu.sa
cd /ibex/your/staging/area
# run your download script
```

> `ibex` · `outside dashboard`

---

**Step 2 — Verify the artifact contents**

Inspect the downloaded files — check the directory structure, file count, and that the contents match what was expected. Unpack archives if necessary.

> `ibex` · `outside dashboard`

---

**Step 3 — Generate a checksum**

Create a SHA-256 checksum file for the artifact. This file travels with the artifact and is used to verify integrity at every transfer step.

```bash
# inside the artifact directory
cd artifact_directory/
find ibex/ai/reference/CV/tinyimagenet/ -type f -print0 | sort -z | xargs -0 sha256sum > checksum.sha256
# verify it was created correctly
sha256sum -c checksum.sha256
```

> `ibex` · ✅ `creates checksum.sha256`

---

**Step 4 — Transfer artifact + checksum to Shaheen3 cpup**

Move the artifact directory and its checksum file together to the staging area on Shaheen3's CPU partition. The destination path follows the convention: `support_team/6####/datasets|models|software/artifact_name/`

```bash
# from Ibex — transfer to sh3 staging
rsync -avP \
  artifact_directory/ \
  x_mohameta@shaheen3.hpc.kaust.edu.sa:\
  /scratch/project/k03/support_team/6####/models/artifact_name/
```

> `ibex → sh3 cpup` · `outside dashboard`

---

### Phase 2 — Shaheen3 cpup

**Step 5 — Verify the checksum on Shaheen3**

After the transfer completes, verify the checksum on Shaheen3 to confirm the files arrived intact. If verification fails, re-transfer from Ibex.

```bash
# on sh3 login node
cd /scratch/project/k03/support_team/6####/models/artifact_name/
sha256sum -c checksum.sha256
# all files should show: OK
```

> `sh3 cpup` · ✅ `must pass before proceeding`

---

**Step 6 — Open an RT ticket for data movement**

Open a ticket in the RT ticketing system for the data movement request. Note the ticket number — you will need it in the dashboard. The form cannot be submitted without it.

> ⚠️ `required — RT ticket number gates first save`

---

**Step 7 — Launch the dashboard**

Submit the Slurm job from any directory on Shaheen3. The job prints the SSH tunnel command and URL to the log file.

```bash
# submit the job
sbatch /scratch/project/k03/support_team/launch_metadata/metadata-dashboard.slurm

# watch the log for connection instructions
tail -f /scratch/project/k03/support_team/launch_metadata/logs/dashboard_<JOBID>.log
```

> 📋 **Dashboard — log output**
> The log will print a line like:
> ```
> ssh -L 8543:nid001234:8543 x_mohameta@login.hpc.kaust.edu.sa
> ```
> Run that command on your laptop, then open `http://localhost:8543`

---

**Step 8 — Sign in as Curator**

The dashboard resolves your role automatically from your Shaheen3 system group membership. You will see the Curator workspace with access to the New Artifact and Existing Artifacts tabs.

> `must be in hpc_support group` · ✅ `role auto-detected`

---

**Step 9 — Register the artifact — New or Import**

You have two paths depending on whether this is a new artifact or an existing hand-authored YML.

> ✨ **Dashboard — New artifact (recommended path)**
> Go to **1 — Curate → New artifact tab**. Enter the source path and click **Auto-fill** — the dashboard extracts the artifact name, type, AIMCR reference, size, and checksum filename automatically. Fill remaining fields, enter the RT ticket number, tick checksum verified on cpup, and click **Stage artifact**.

> 📥 **Dashboard — Existing YML (import path)**
> Go to **4 — Import**. Paste the full path to the existing YML file and click **Load file**. The dashboard reads the file tolerantly, flags any duplicate keys or missing fields, and pre-fills the form. Fill any gaps, then click **Import artifact**.

On successful save the dashboard:
- Writes YML to disk
- Creates modlog sidecar
- Appends to `manifest.yml`
- Registers in DB

---

**Step 10 — Edit metadata at any time**

Go to **Existing artifacts**, find the artifact in the table, and click Open. All identity and curation fields are editable. Every save records a modification log entry showing exactly which fields changed and from what value to what value.

> 🔒 **Dashboard — field ownership**
> As curator you can edit all identity and curation fields at any time. Moving fields (`moved_by`, `date_moved`, gpup checksum) are read-only for you — those belong to the mover.

---

**Step 11 — Block or reject if something is wrong**

If a problem is discovered — bad checksum, wrong path, quota exceeded — open the artifact detail, expand **Actions**, select **Block**, enter a reason, and click **Apply action**. The status changes to `blocked` and the data movers will see the reason. To unblock, select **Reinstate**.

> ⚠️ `blocked — temporary hold, resolvable`
> ⚠️ `rejected — soft refusal, reinstatable`

---

## Mover — Data Mover

*Transfers staged artifacts from cpup to gpup. Verifies integrity on arrival. Confirms the move.*

### Phase 1 — Prepare

**Step 1 — Launch the dashboard**

Submit the Slurm job and follow the printed tunnel instructions — same as the curator.

```bash
sbatch /scratch/project/k03/support_team/launch_metadata/metadata-dashboard.slurm
tail -f /scratch/project/k03/support_team/launch_metadata/logs/dashboard_<JOBID>.log
```

---

**Step 2 — Find staged artifacts**

Go to **2 — Move**. The table shows only artifacts with status `staged` or `blocked`. Use the search box and filters to find the artifact you need to move.

> 🔒 **Dashboard — read-only curation fields**
> When you open an artifact, all curation fields filled by the curator are shown read-only. You can see the source path, checksum details, curation notes, and any blocking reasons — but cannot edit them.

---

### Phase 2 — Transfer

**Step 3 — Verify the destination path is accessible**

Before transferring, confirm the destination path exists and is writable. The dashboard checks this and shows a green confirmation when the path is reachable.

```bash
# manual check on sh3
ls /scratch/project/k#####/models/
lfs quota -u $USER /scratch
```

> ⚠️ `check quota before transferring large artifacts`

---

**Step 4 — Transfer artifact + checksum from cpup to gpup**

Run the transfer outside the dashboard. Transfer both the artifact and its checksum file together.

```bash
# transfer from cpup staging to gpup destination
rsync -avP \
  /scratch/project/k03/support_team/6####/models/artifact_name/ \
  /scratch/project/k#####/models/artifact_name/
```

> `sh3 cpup → sh3 gpup` · `outside dashboard`

---

**Step 5 — Verify the checksum on gpup**

After the transfer, run the checksum verification at the destination. Every file must pass before confirming the move in the dashboard.

```bash
# at the destination on gpup
cd /scratch/project/k#####/models/artifact_name/
sha256sum -c checksum.sha256
# all files must show: OK
```

> ✅ `must pass before confirming in dashboard`

---

### Phase 3 — Confirm in Dashboard

**Step 6 — Fill moving fields**

Return to the artifact in the dashboard. In the **Moving fields** section, tick **Verified on gpup** and enter your username. Add any moving notes. Click **Save moving fields**.

> ✏️ **Dashboard — your fields to fill**
> `checksum_verified_gpup` (verified + verified_by) and `moving_notes`. These can be edited after confirmation if you make a mistake — every edit is logged with a diff.

---

**Step 7 — Confirm the move**

Expand the **Confirm move** section and click **Confirm move**. The dashboard sets the status to `moved`, records your username and timestamp, updates the partition to `gpu`, writes the YML snapshot, and syncs the manifest.

> ✅ **Dashboard — what happens on confirm**
> `status → moved` · `partition → gpu` · `date_moved` auto-set · `moved_by` auto-captured · manifest entry updated · modification log appended

On confirm the dashboard:
- Updates YML on disk
- Syncs manifest
- Updates DB
- Appends modlog entry

---

**Step 8 — Block if something goes wrong**

If the destination is inaccessible, the checksum fails, or quota is exceeded — use the **Actions** section to block the artifact with a reason. The curator will see the reason. Use **Reinstate** when the issue is resolved.

> ⚠️ `reason required to block`
> ⚠️ `reason propagated to modification log`

---

## Browser — Read-Only Access

*PIs, collaborators, and guests. Full visibility into artifact status and metadata. No write access.*

### Access

**Step 1 — Launch the dashboard**

Submit the Slurm job and follow the printed tunnel instructions. You need a Shaheen3 account and VPN or KAUST network access.

```bash
sbatch /scratch/project/k03/support_team/launch_metadata/metadata-dashboard.slurm
tail -f /scratch/project/k03/support_team/launch_metadata/logs/dashboard_<JOBID>.log
# then open http://localhost:<PORT>
```

---

**Step 2 — Browse all artifacts**

Navigate to **3 — Browse**. All artifacts across all projects are listed. Filter by name, type (`dataset` / `model` / `software`), or status (`staged` / `moved` / `blocked` / `rejected`).

> 🔍 **Dashboard — what you can see**
> Full artifact metadata — identity, curation details, paths, checksum verification status, moving details, status history, and the complete modification log showing who changed what and when.

---

**Step 3 — View the modification log**

Open any artifact and expand **Modification log** to see a full timeline of every action taken — staged, moved, blocked, updated — with the actor's username, timestamp, and a diff of what changed.

> `read-only` · `no write access on any page`

---

*Metadata Dashboard · Shaheen3 HPC · KAUST · k03 · Support team: help@hpc.kaust.edu.sa*
