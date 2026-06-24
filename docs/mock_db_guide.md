# Switching to a mock database for testing

`config.py` is the single place to change. Find this line:

```python
DB_PATH: Path = Path(
    "/scratch/project/k03/artifact_db/tracker.db"
)
```

Change it to any writable path:

```python
DB_PATH: Path = Path(
    "/scratch/project/k03/mock_db/tracker_test.db"
)
```

The app creates the database automatically on startup if it does not exist — no migration needed.

---

## Update the Slurm script

The Singularity container must bind-mount the directory where the test database lives.
Find the `DB_DIR` line in `slurm_launcher/metadata-dashboard.slurm` and update it to match `config.py`:

```bash
DB_DIR="/scratch/project/k03/support_team/mock_db:/scratch/project/k03/support_team/mock_db"
```

Make sure the directory exists before submitting:

```bash
mkdir -p /scratch/project/k03/support_team/mock_db
```

The `--bind "${DB_DIR}` line in the `singularity exec` call handles the mount — no other changes needed in the script.

---

## Resetting the test database

```bash
# wipe and start fresh
rm -f /scratch/project/k03/support_team/mock_db/tracker_test.db
rm -f /scratch/project/k03/support_team/mock_db/tracker_test.db-shm
rm -f /scratch/project/k03/support_team/mock_db/tracker_test.db-wal
```

Restart the job — a clean empty database is created on next launch.

---

## Switching back to production

Revert `config.py` to the original path, clear pycache, restart the job.

```bash
find /scratch/project/k03/support_team/Metadata_Dashboard -name "*.pyc" -delete
find /scratch/project/k03/support_team/Metadata_Dashboard -name "__pycache__" \
    -type d -exec rm -rf {} + 2>/dev/null
scancel $(squeue -u $USER -h -o "%i")
sbatch /scratch/project/k03/support_team/launch_metadata/metadata-dashboard.slurm
```

Also revert `DB_DIR` in the Slurm script back to the production path:

```bash
DB_DIR="/scratch/project/k03/support_team/Metadata_Dashboard"
```

---

> **Note** — the YML files and manifests on disk are unaffected by the database switch.
> The test database is just a fresh index. Any YMLs you stage during testing
> are real files written to disk — clean them up manually if needed:
> ```bash
> rm /scratch/project/k03/support_team/<aimcr>/<type>/<artifact>.yml
> rm /scratch/project/k03/support_team/<aimcr>/<type>/<artifact>.modlog.yml
> ```
