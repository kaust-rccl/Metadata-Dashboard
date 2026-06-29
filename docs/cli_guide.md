# `dashboard` CLI — Guide

**Shaheen3 · KAUST · k03**  
Terminal interface for both pipelines. Changes appear in the UI on next refresh.

---

## Setup

```bash
echo 'export PATH="/scratch/project/k03/support_team/Metadata_Dashboard/cli:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

---

## HPC-AI pipeline

### Commands

| Command | What it does |
|---|---|
| `dashboard list` | List artifacts — filter with `--status`, `--aimcr`, `--type`, `--json` |
| `dashboard inspect <aimcr> <name>` | Full artifact detail and modlog |
| `dashboard move <aimcr> <name>` | Confirm move — sets `status=moved`, `partition=gpu`, records timestamp |
| `dashboard block <aimcr> <name> "<reason>"` | Block with reason — curator sees it in the dashboard |
| `dashboard reinstate <aimcr> <name> "<reason>"` | Reinstate blocked or rejected → `staged` |
| `dashboard edit <aimcr> <name> field=value` | Edit mover fields: `destination_path`, `project_id`, `moving_notes`, `checksum_verified_gpup` |

Every write updates the YML on disk, the database, the modlog (with a diff), and the manifest.

### Typical workflow

```bash
# find what needs moving
dashboard list --status staged

# inspect — check source path, size, curation notes
dashboard inspect 65616 Seed-X-PPO-7B

# fill in destination and project id if curator left placeholders
dashboard edit 65616 Seed-X-PPO-7B \
  destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B" \
  project_id="k00123"

# transfer and verify — outside the dashboard
rsync -avP \
  /scratch/project/k03/support_team/65616/models/Seed-X-PPO-7B/ \
  /scratch/project/k00123/models/Seed-X-PPO-7B/
cd /scratch/project/k00123/models/Seed-X-PPO-7B/
sha256sum -c checksum.sha256   # all files must show: OK

# confirm the move
dashboard move 65616 Seed-X-PPO-7B \
  --verified-by your_username \
  --notes "transferred and verified on gpup"
```

### Quick reference

```bash
dashboard list
dashboard list --status staged --aimcr 65616 --json
dashboard inspect 65616 Seed-X-PPO-7B
dashboard move 65616 Seed-X-PPO-7B --verified-by mover1 --notes "ok"
dashboard block     65616 Seed-X-PPO-7B "quota exceeded"
dashboard reinstate 65616 Seed-X-PPO-7B "resolved"
dashboard edit 65616 Seed-X-PPO-7B destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B"
dashboard edit 65616 Seed-X-PPO-7B project_id="k00123"
dashboard edit 65616 Seed-X-PPO-7B moving_notes="re-verified after network drop"
```

---

## HPC pipeline

### Commands

| Command | What it does |
|---|---|
| `dashboard hpc list` | List transfers — filter with `--status`, `--ticket`, `--json` |
| `dashboard hpc inspect <id>` | Full transfer detail, verification counts, audit log, YML modlog |
| `dashboard hpc new` | Create a transfer record. Required: `--ticket`, `--name`, `--src`, `--dst`, `--requester` |
| `dashboard hpc premove <id>` | Generate `generate_checksum.slurm` → workspace. Status → `pre-move`. Prints sbatch command. |
| `dashboard hpc postmove <id>` | Generate `verify_checksum.slurm` → workspace. Status → `verifying`. Prints sbatch command. |
| `dashboard hpc verify <id>` | Parse `verify_result.txt`, record counts, status → `completed` or `anomaly`. Use `--result` to override path. |
| `dashboard hpc edit <id> field=value` | Edit any transfer field. Sync warning if scripts already exist. |
| `dashboard hpc block <id> "<reason>"` | Block with reason |
| `dashboard hpc reinstate <id> "<reason>"` | Reinstate to `pending` |
| `dashboard hpc status <id> <status>` | Manually set any status |

Every write updates the database and mirrors changes into `workspace/artifact.yml` and `artifact.modlog.yml`.

### Typical workflow

```bash
# 1. create the record
dashboard hpc new \
  --ticket RT-12345 \
  --name imagenet-val \
  --src /scratch/project/k03/support_team/65638/datasets/imagenet-val \
  --dst /scratch/project/k00456/datasets/imagenet-val \
  --requester pi_user \
  --type dataset \
  --size 142.5

# 2. generate and submit checksum script
dashboard hpc premove 7
sbatch /scratch/project/k03/support_team/workspace/RT-12345_imagenet-val/generate_checksum.slurm
# wait for job to complete: squeue --me

# 3. move the data
rsync -avP /source/path/ /destination/path/

# 4. generate and submit verify script
dashboard hpc postmove 7
sbatch /scratch/project/k03/support_team/workspace/RT-12345_imagenet-val/verify_checksum.slurm
# wait for job to complete: squeue --me

# 5. import result
dashboard hpc verify 7
```

### Handling anomalies

```bash
# check which files failed
grep "FAILED" /scratch/project/k03/support_team/workspace/RT-12345_imagenet-val/verify_result.txt

# re-rsync failed files
rsync -avP /source/failed_file /destination/

# regenerate verify script and re-run
dashboard hpc postmove 7
sbatch .../verify_checksum.slurm
dashboard hpc verify 7
```

### Sync warnings on edit

If you edit `source_path`, `destination_path`, `ticket_number`, or `artifact_name` after scripts have already been generated, the CLI prints a warning:

```
! generate_checksum.slurm is out of sync — affected: {'source_path'}
! Regenerate with: dashboard hpc premove 7
```

The record is still saved. Regenerate the affected script from the indicated command.

### Quick reference

```bash
dashboard hpc list
dashboard hpc list --status pending --json
dashboard hpc inspect 7

dashboard hpc new --ticket RT-12345 --name imagenet-val \
  --src /cpup/path --dst /gpup/path --requester pi_user \
  --type dataset --size 142.5

dashboard hpc premove 7
dashboard hpc postmove 7
dashboard hpc verify 7
dashboard hpc verify 7 --result /custom/path/verify_result.txt

dashboard hpc edit 7 destination_path="/scratch/project/k00456/datasets/imagenet-val"
dashboard hpc edit 7 notes="requester confirmed destination"

dashboard hpc block     7 "quota exceeded on gpup"
dashboard hpc reinstate 7 "quota resolved"
dashboard hpc status    7 in-progress
```