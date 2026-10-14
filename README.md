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
│   ├── services.py         # one class per lifecycle action
│   ├── sync.py             # two-way YML ↔ DB sync (startup + `dashboard sync`)
│   ├── projects.py         # project profile lookup for auto-fill
│   └── perms.py            # group k03 rwx on every YML / lock / dir created
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
    ↓ mirrored on every write
SQLite DB           ←  read-optimised mirror (dashboard queries)
    ↓ sidecar
artifact.modlog.yml ←  best-effort audit mirror
    ↓ summary
manifest.yml        ←  per-project index
```

## Sync

`iolib/sync.py` reconciles YMLs and the DB in both directions. It runs once
per dashboard session and on demand via `dashboard sync [--dry-run]`.

- **YML, no DB row** → row inserted.
- **DB row, no YML** → YML written at the canonical path
  `PROJECTS_BASE/<aimcr>/<datasets|models|software>/<name>.yml`, plus a modlog
  entry and a manifest entry. If the row's stored `yml_path` points at a valid
  record elsewhere (old Import-page rows), that file is copied; otherwise the
  record is rebuilt from DB columns and the modlog lists the fields the DB
  does not hold.
- **Both, different path** → DB row repointed (modlog_entries follow).

Only `PROJECTS_BASE/*/{datasets,models,software}/*.yml` is visited. A file is
treated as ours only if it is a regular file (no symlinks), under 256 KB,
has top-level `artifact_name`, `artifact_type` and `aimcr_reference` keys
in a plain-text check, and then passes `models.artifact.is_artifact_yml`.
Anything else is never parsed and is listed as ignored.

Sync never overwrites a file and never guesses. These are reported as
conflicts for a human: a record sitting somewhere other than where its
content says; a canonical path occupied by another file; several DB rows
with one identity; DB rows with invalid enum values, unsafe names, or an
AIMCR reference not matching `AIMCR_REFERENCE_PATTERN`.


### Duplicates

Several DB rows sharing a `yml_path` or an (AIMCR reference, type, name) are
flagged in every artifact table (⚠ and a warning). Sync splits them: one row
keeps the record, every other row gets its own name — `<name>_dup2`,
`_dup3`, … — and its own YML, and is listed under "Duplicates split" for
review. Delete the ones that were true duplicates. Once no `yml_path` is
shared, sync adds the missing unique index on `artifacts.yml_path`, after
which the DB itself refuses new duplicates.

## Deleting a record

Curate page → artifact → **Delete record** (curator / admin; reason and the
artifact name required), or `dashboard delete <aimcr> <name> "<reason>"`.
Removes the YML, the DB row(s) and the manifest entry. The data at the
source/destination paths is never touched and the modlog is kept, ending in
a `deleted` entry — sync never recreates a record whose modlog ends that way,
and finishes a delete that was interrupted.

## Project profile (auto-fill)

Proposal title, KAUST project ID (k####) and PI are stored per AIMCR
reference — in `manifest.yml` (`kaust_project_id`) and the `projects` table —
every time an artifact is saved. On the Curate page they fill in as soon as
the AIMCR reference is known (from the source path, or typed into
"Load project details"); the Import page fills them where the source file
has blanks. Only empty fields are ever filled. Proposals staged before this
change are covered too: the newest artifact YML of the proposal is used.

## File permissions

Every YML, modlog, manifest and lock file the app creates gets group `k03`
(`FILE_GROUP` in `config.py`) and `u+rwx,g+rwx`; every directory it creates
gets the same plus setgid, so files created inside later inherit the group.
Bits are only added. Existing files are fixed when they are next rewritten.