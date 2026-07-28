# Run files, config, and cleanup

Where each run leaves its files, the two config files, and what is safe to delete.

## Config files

- **`config/migration.yaml`** — the saved run config: connection details, `SRC_DBS`,
  mode, and flags. Passwords are redacted by default when a config is saved.
  Edit it directly to customise a run, then re-run non-interactively:

  ```bash
  source .venv/bin/activate
  SRC_ADMIN_PASS=… TGT_ADMIN_PASS=… \
    python3 -m orchestrator.migrationctl run \
    --config config/migration.yaml --mode <mode> --out artifacts/run
  ```

- **`sqldata.cfg`** — the `mariadb-mtk` config. It is read from the directory the
  launcher is started in, so run `mariadb-migrator` from the repo root.
  `sqldata.cfg-example` is the template.

## Run artifacts

Each run writes to its own timestamped directory:

```
artifacts/
└── run_<mode>_<ts>/          # one directory per run
    ├── run.log               # full run log — tail -f for live progress
    ├── report.json           # assess + gate results (JSON)
    ├── mariadb-mtk/<db>/      # mariadb-mtk output and per-database logs
    ├── binlog_coords.env      # Replication mode only
    └── manifest.txt + dumps   # Offline Staged mode only
```

## Reading a run

```bash
# live progress
tail -f artifacts/run_two_step_<ts>/run.log

# what failed (gate status is uppercase PASS/FAIL)
jq '.gates[] | select(.status=="FAIL")' artifacts/<run>/report.json

# scan the log
grep -niE 'error|denied|fail' artifacts/<run>/run.log
```

## Cleanup

```bash
rm -rf artifacts   # drops all run history; recreated on the next run
rm -rf .venv       # resets the Python env; rebuilt on next ./mariadb-migrator
rm -f  state.json  # resume pointer; delete to forget the last run (artifacts remain)
```

> **Note:** cleanup is destructive. If a run is unfinished or you may still want to
> resume or inspect it, this information is still relevant to that run and will be
> lost once `artifacts/` (and `state.json`) are removed. Clean up only runs you are
> done with.
