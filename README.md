# Artifact Tracker — Shaheen3

Streamlit dashboard for tracking HPC artifact curation and movement.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py --server.port 8501
```

Access via SSH tunnel from your laptop:

```bash
ssh -L 8501:localhost:8501 username@shaheen3.kaust.edu.sa
# then open http://localhost:8501
```

## Before deployment

1. Edit `config.py` — update these values for your environment:
   ```python
   STAGING_BASE        = Path("/your/staging/path")
   PROJECTS_BASE       = Path("/scratch/project")
   DB_PATH             = Path("/your/db/path/tracker.db")
   LDAP_GROUP_CURATOR  = "your_hpc_support_group"
   LDAP_GROUP_MOVER    = "your_data_movers_group"
   ```

2. Ensure the process owner has read/write access to `PROJECTS_BASE` and `DB_PATH`.

## Project structure

```
dashboard/
├── app.py                  # entry point — routing and session init only
├── config.py               # all constants, enums, path helpers
├── requirements.txt
│
├── models/                 # pure dataclasses — no I/O, no Streamlit
│   ├── artifact.py         # ArtifactYML — canonical metadata record
│   ├── modlog.py           # ArtifactModLog — sidecar mirror
│   └── manifest.py         # ManifestYML — per-project index
│
├── iolib/                  # all I/O — yaml, filesystem, database, services
│   ├── yaml_io.py          # atomic write, file lock, ruamel.yaml helpers
│   ├── fs.py               # path checks, size, checksum verification
│   ├── database.py         # SQLite interface — read-optimised mirror
│   └── services.py         # one class per lifecycle action
│
├── auth/
│   ├── ldap.py             # Unix group → role resolution
│   └── session.py          # Streamlit session state helpers
│
├── components/             # reusable Streamlit widgets
│   ├── fields.py           # field renderers with role-based locking
│   ├── modlog_viewer.py    # modlog timeline display
│   └── artifact_table.py  # filterable artifact listing
│
└── pages/                  # one file per page — thin wrappers over services
    ├── 1_curate.py         # curator: stage, edit notes, block/reject/reinstate
    ├── 2_move.py           # mover: confirm move, block/reject/reinstate
    └── 3_browse.py         # read-only: all roles
```

## Adding a new feature

- **New lifecycle action** → add a service class in `iolib/services.py`
- **New field** → add to `ArtifactYML` in `models/artifact.py`, update `to_dict`/`from_dict`, add DB column in `iolib/database.py`
- **New page** → create `pages/N_name.py`, call `init_session()` at top
- **New role** → add to `Role` enum in `config.py`, add group name constant, update `auth/ldap.py`

## Source of truth hierarchy

```
ArtifactYML (disk)  ←  canonical
    ↓ sync on STAGED/MOVED
SQLite DB           ←  read-optimised mirror (dashboard queries)
    ↓ sidecar
artifact.modlog.yml ←  best-effort audit mirror
    ↓ summary
manifest.yml        ←  per-project index
```
