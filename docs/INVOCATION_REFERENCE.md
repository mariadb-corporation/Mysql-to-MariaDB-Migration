# Invocation reference

The migration tool has two entry points: the `mariadb-migrator` launcher
(an interactive bash wrapper) and the `orchestrator.migrationctl` Python
module (the underlying orchestrator). The launcher is the recommended
entry point for human operators; the Python module is useful for
automation, CI pipelines, and debugging.

## The `mariadb-migrator` launcher

The launcher walks the operator through mode selection, source/target
credentials, optional config save, and the three migration phases
(assess → plan → run). It is the canonical entry point.

```bash
# Default: walk all three phases interactively.
./mariadb-migrator

# Run a single phase. Mode selection and credential prompts still apply.
./mariadb-migrator --assess
./mariadb-migrator --plan
./mariadb-migrator --run

# Help.
./mariadb-migrator --help
```

### Common environment variables

The launcher honors a number of environment variables that bypass
interactive prompts. Setting these is useful for scripted invocations
or re-running with the same configuration.

| Variable | Purpose |
|---|---|
| `MODE` | Migration mode (`one_step`, `two_step`, `binlog`, `staged`). Skips the mode-selection menu. |
| `TWO_STEP_VARIANT` | For `MODE=two_step`: `single_pass` (default) or `resumable`. |
| `STAGED_PHASE` | For `MODE=staged`: `dump_and_load` (default), `dump_only`, or `load_only`. |
| `FORCE_NEW_RUN` | Set to `1` to ignore previous-run state and start fresh. |
| `SRC_HOST`, `SRC_PORT`, `SRC_ADMIN_USER`, `SRC_ADMIN_PASS` | Source MySQL connection. |
| `SRC_DB` or `SRC_DBS` | Single database name, or comma-separated list. |
| `TGT_HOST`, `TGT_PORT`, `TGT_ADMIN_USER`, `TGT_ADMIN_PASS` | Target MariaDB connection. |
| `REPL_USER`, `REPL_PASS` | For `MODE=binlog`: replication user credentials. |
| `ALLOW_TARGET_DB_OVERWRITE` | Set to `1` to permit overwriting an existing database on the target. |

When `MODE` is set in the environment, the launcher's mode-selection
menu is skipped entirely; if a source-side compatibility check then
fails, the launcher exits non-zero rather than re-prompting (since the
mode was operator-asserted, not interactively chosen).

## The `orchestrator.migrationctl` Python module

Each phase is also exposed as a Python subcommand. The launcher invokes
these internally; you can invoke them directly for debugging or
automation. Each subcommand reads its source/target connection details
from a YAML config file (typically `config/migration.yaml`) plus any
overriding environment variables.

```bash
# Assess phase — read-only checks against source. Exits non-zero on FAIL.
python3 -m orchestrator.migrationctl assess \
  --config config/migration.yaml \
  --mode binlog \
  --out artifacts/assess_test

# Plan phase — generates the execution plan from config + step_map.yaml.
python3 -m orchestrator.migrationctl plan \
  --config config/migration.yaml \
  --mode binlog \
  --out artifacts/plan_test

# Run phase — execute the planned migration from scratch.
python3 -m orchestrator.migrationctl run \
  --config config/migration.yaml \
  --mode binlog \
  --out artifacts/run_test

# Resume phase — pick up a previously-started run from its state directory.
# Used for binlog/replace_slave continuation, or to recover from a mid-run
# failure when inputs are unchanged. The launcher picks resume vs run
# automatically based on prior-run state and FORCE_NEW_RUN.
python3 -m orchestrator.migrationctl resume \
  --config config/migration.yaml \
  --mode binlog \
  --out artifacts/existing_run_dir
```

### Notes on direct Python invocation

- **Credentials must be available.** The launcher feeds credentials into
  the Python entrypoint via shell environment variables populated by
  interactive prompts. When you invoke the Python module directly,
  credentials must come from one of:
  - the YAML config file (passwords saved with
    `Include passwords/secrets in saved config? = y`), or
  - environment variables set in your shell before invocation
    (`SRC_ADMIN_PASS=...`, etc.).

  A config saved without passwords plus an unset env var will fail
  authentication with a generic `ERROR 1045 (28000): Access denied`
  message in `artifacts/.../report.json`.

- **`--mode` enables mode-aware gates.** The `assess` subcommand accepts
  `--mode` so that gates conditioned on the chosen migration mode
  (currently the binlog source-compatibility gate) can evaluate
  correctly. If `--mode` is omitted, mode-conditional gates do not
  run; assess still reports source inventory and warnings, but cannot
  warn about mode-specific incompatibilities.

- **Exit codes.** Each subcommand returns `0` on success and non-zero on
  failure. Assess uses exit code `2` for hard-gate failures. Run-phase
  preflight scripts use granular exit codes — see
  `scripts/00_preflight_binlog.sh` for the binlog mode's exit code
  mapping (`9` for `binlog_format`-not-`ROW`, `10` for JSON columns,
  etc.).

- **Artifacts.** Each invocation writes to a directory under `artifacts/`
  containing `report.json` (machine-readable result), `run.log` (full
  log), and a `pre/` subdirectory with per-check TSV outputs. The
  report's `gates` array enumerates every gate evaluated, its status
  (`PASS`/`FAIL`), and the details collected — useful for debugging
  why an assessment failed when the stdout message is terse.

## Example: scripted assess against a non-default config

```bash
SRC_ADMIN_PASS='...' \
TGT_ADMIN_PASS='...' \
python3 -m orchestrator.migrationctl assess \
  --config config/staging-cluster.yaml \
  --mode binlog \
  --out artifacts/assess_staging_$(date +%Y%m%d_%H%M%S)
```

Exit code reflects pass/fail. The artifact directory is preserved for
inspection regardless of outcome.
