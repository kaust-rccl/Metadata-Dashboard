# `dashboard` CLI — Mover Guide

**Shaheen3 · KAUST · k03**  
Terminal interface for data movers. All changes sync to the UI on next refresh.

---

## Setup

```bash
echo 'export PATH="/scratch/project/k03/support_team/Metadata_Dashboard/cli:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

---

## Commands

| Command | What it does |
|---|---|
| `dashboard list` | List artifacts — filter with `--status`, `--aimcr`, `--type`, `--json` |
| `dashboard inspect <aimcr> <name>` | Show full artifact detail and modlog |
| `dashboard move <aimcr> <name>` | Confirm move — sets `status=moved`, `partition=gpu`, records timestamp |
| `dashboard block <aimcr> <name> "<reason>"` | Block with a reason — curator sees it in the dashboard |
| `dashboard reinstate <aimcr> <name> "<reason>"` | Reinstate blocked or rejected → `staged` |
| `dashboard edit <aimcr> <name> field=value` | Edit mover fields — see below |

**Editable fields via `edit`:** `destination_path` · `project_id` · `moving_notes` · `checksum_verified_gpup`

Every write updates the YML on disk, the database, the modlog (with a diff), and the manifest.

---

## Typical workflow

```bash
# 1. find what needs moving
dashboard list --status staged

# 2. inspect — check source path, size, curation notes
dashboard inspect 65616 Seed-X-PPO-7B

# 3. fill in destination and project id if curator left placeholders
dashboard edit 65616 Seed-X-PPO-7B \
  destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B" \
  project_id="k00123"

# 4. transfer and verify — outside the dashboard
rsync -avP \
  /scratch/project/k03/support_team/65616/models/Seed-X-PPO-7B/ \
  /scratch/project/k00123/models/Seed-X-PPO-7B/

cd /scratch/project/k00123/models/Seed-X-PPO-7B/
sha256sum -c checksum.sha256   # all files must show: OK

# 5. confirm the move
dashboard move 65616 Seed-X-PPO-7B \
  --verified-by your_username \
  --notes "transferred and verified on gpup"
```

---

## Handling problems

```bash
# something is wrong — block it with a reason
dashboard block 65616 Seed-X-PPO-7B "quota exceeded on gpup"

# resolved — reinstate
dashboard reinstate 65616 Seed-X-PPO-7B "quota resolved"

# wrong notes after confirmation — fix them
dashboard edit 65616 Seed-X-PPO-7B moving_notes="re-verified after network drop"
```

---

## Quick reference

```bash
dashboard list
dashboard list --status staged --aimcr 65616 --json

dashboard inspect 65616 Seed-X-PPO-7B

dashboard move 65616 Seed-X-PPO-7B --verified-by mover1 --notes "ok"

dashboard block     65616 Seed-X-PPO-7B "reason"
dashboard reinstate 65616 Seed-X-PPO-7B "reason"

dashboard edit 65616 Seed-X-PPO-7B destination_path="/scratch/project/k00123/models/Seed-X-PPO-7B"
dashboard edit 65616 Seed-X-PPO-7B project_id="k00123"
dashboard edit 65616 Seed-X-PPO-7B moving_notes="re-verified"
```
