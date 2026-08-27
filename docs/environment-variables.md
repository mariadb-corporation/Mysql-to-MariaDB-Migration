# Environment Variables Reference

This document is the canonical reference for environment variables consumed by the migration tool. Variables are organized by section. Each entry shows the name, default value (where applicable), and a short description.

For mode-specific *required* variables, see the corresponding section of `README.md`. This document covers what each variable means and how it's used; the README covers which ones you must set for a given mode.

> **Scope.** This reference covers public-facing variables — ones a user might reasonably set in `config/migration.yaml`, in the shell environment, or via the interactive launcher. Internal implementation variables (tool binary paths, internal flags) are not documented here; see the scripts in `scripts/` for the complete picture.

## Conventions

- **Boolean variables** use `"1"` for true and `"0"` for false unless noted. They are quoted strings in YAML (the orchestrator reads them as strings).
- **Path variables** can be absolute or relative to the orchestrator working directory.
- **Variables with a "Deprecated alias" line** accept an older name for backward compatibility; the script will emit a deprecation notice if the old name is used.
- **Empty default (`""`)** means the variable has no built-in default and must be set explicitly when required.

---

## Source connection

| Name | Default | Description |
|---|---|---|
| `SRC_HOST` | `""` | Source MySQL host (FQDN or IP). Required for all modes except `staged` in `load_only` phase. |
| `SRC_PORT` | `"3306"` | Source MySQL port. |
| `SRC_ADMIN_USER` | `""` | Admin user on the source. Used for dump operations and pre-flight checks. |
| `SRC_ADMIN_PASS` | `""` | Password for `SRC_ADMIN_USER`. Redacted when saved to `config/migration.yaml`. |
| `SRC_USER` | `""` | Alias for `SRC_ADMIN_USER` (legacy; `SRC_ADMIN_USER` takes precedence). |
| `SRC_PASS` | `""` | Alias for `SRC_ADMIN_PASS` (legacy; `SRC_ADMIN_PASS` takes precedence). |
| `SRC_DB` | `""` | Single source database to migrate. Use when only one DB is in scope. |
| `SRC_DBS` | `""` | Comma-separated list of source databases (e.g. `"sakila,world"`). Takes precedence over `SRC_DB`. |
| `SRC_SSL_MODE` | `""` | TLS mode for source connection. Accepts `DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY`. Required for TLS-mandatory sources like AWS RDS / Aurora. |

## Target connection

| Name | Default | Description |
|---|---|---|
| `TGT_HOST` | `""` | Target MariaDB host (FQDN or IP). Required for all modes except `staged` in `dump_only` phase. |
| `TGT_PORT` | `"3306"` | Target MariaDB port. |
| `TGT_ADMIN_USER` | `""` | Admin user on the target. Used for load operations and target-side configuration. |
| `TGT_ADMIN_PASS` | `""` | Password for `TGT_ADMIN_USER`. Redacted when saved to `config/migration.yaml`. |
| `TGT_USER` | `""` | Alias for `TGT_ADMIN_USER` (legacy; `TGT_ADMIN_USER` takes precedence). |
| `TGT_PASS` | `""` | Alias for `TGT_ADMIN_PASS` (legacy; `TGT_ADMIN_PASS` takes precedence). |

> **Note on target TLS.** Target connections currently negotiate TLS where the server requires it (e.g. MariaDB Cloud), but server-certificate verification is not yet configurable. Connections are encrypted in transit but not authenticated against a trusted CA. Configurable target-side TLS (`TGT_SSL_MODE`, `TGT_SSL_CA`) is planned for a future release. See the corresponding GitHub issue for status.

## Target SSH (when orchestrator runs on a third host)

| Name | Default | Description |
|---|---|---|
| `TGT_SSH_HOST` | `""` | SSH hostname of the target. Required for the deprecated install path and for `replace_slave` mode. Not needed for `staged`, `one_step`, `two_step`, or `binlog` modes against a pre-installed target. |
| `TGT_SSH_USER` | `"root"` | SSH user for connecting to `TGT_SSH_HOST`. |
| `TGT_SSH_OPTS` | `""` | Additional `ssh` flags. Common: `"-o StrictHostKeyChecking=no"` for first-time CI connections. |

## Common runtime

| Name | Default | Description |
|---|---|---|
| `ALLOW_ROOT_USERS` | `"0"` | Set to `"1"` to allow `root` as `SRC_ADMIN_USER` or `TGT_ADMIN_USER`. Default rejects root for safety. |
| `ALLOW_TARGET_DB_OVERWRITE` | `"0"` | Set to `"1"` to allow loading into a target where the destination databases already exist. Default fails fast to prevent accidental overwrite. |
| `MIGRATE_APP_USERS` | `"0"` | Set to `"1"` to migrate application users from source to target as part of the run. |
| `APP_USER_DEFAULT_PASSWORD` | *(prompted)* | Password assigned to accounts whose original password can't be carried over. Recorded in plain text in `user_migration_report.txt`. |
| `APP_USER_PWD_EXPIRE` | `"1"` | Set to `"0"` so default-password accounts are not forced to change their password at first login. Accounts whose original password was carried over are never affected. |
| `PORT_SHA2_PASSWORDS` | `"1"` | Set to `"0"` to skip carrying over passwords for `caching_sha2_password` accounts and give them the default password instead. |
| `PORT_SHA2_INSTALL_PLUGIN` | `"0"` | Set to `"1"` to let the tool load the `caching_sha2_password` plugin on the target when it isn't already loaded. Requires MariaDB 11.4.9 / 11.8.4 or later. |
| `STRIP_DEFINERS` | `"1"` | Strip `DEFINER=` clauses from dumps so views/procedures load on targets where the source DEFINER doesn't exist. |

## Binlog mode

| Name | Default | Description |
|---|---|---|
| `REPL_USER` | `""` | Replication user that MariaDB will use to read MySQL binlog. Required. |
| `REPL_PASS` | `""` | Password for `REPL_USER`. Required. |
| `SRC_BINLOG_FILE` | `""` | Binlog filename to start replication from. Auto-captured during seed if not set. |
| `SRC_BINLOG_POS` | `""` | Binlog position to start replication from. Auto-captured during seed if not set. |
| `BINLOG_COORD_FILE` | `"artifacts/binlog_coords.env"` | Path where the captured binlog coordinates are written during seed. |
| `BINLOG_MAX_LAG_SECS` | `"30"` | Maximum acceptable replication lag (in seconds) for the post-start verification step. |
| `BINLOG_VERIFY_POLL_SECS` | `"3"` | How often the verification step polls replication status. |
| `BINLOG_VERIFY_TIMEOUT_SECS` | `"90"` | How long the verification step waits for replication to come into compliance with `BINLOG_MAX_LAG_SECS`. |
| `BINLOG_CREATE_REPL_USER` | `"1"` | Whether to create the replication user on the source as part of seed. Set to `"0"` if the user already exists. |
| `BINLOG_AUTO_FIX_SERVER_ID` | `"1"` | Whether to auto-assign a target `server_id` to avoid collision with the source. |
| `BINLOG_MASTER_SSL` | `"0"` | Whether MariaDB-as-replica should use TLS when connecting to the MySQL source. |
| `BINLOG_MASTER_SSL_VERIFY_SERVER_CERT` | `"0"` | Whether MariaDB-as-replica should verify the source's TLS certificate. |

## Two-step mode

| Name | Default | Description |
|---|---|---|
| `SQLINESDATA_BIN` | `""` | Path to the `sqldata` / `sqlinesdata` binary. Auto-detected on `PATH` if unset. |
| `TWO_STEP_KEEP_FK_CHECKS` | `"0"` | Set to `"1"` to keep foreign-key checks enabled during parallel data load. Default disables them for performance; see README for the manual DBA toggle. |

## Staged mode

| Name | Default | Description |
|---|---|---|
| `STAGED_PHASE` | `"dump_and_load"` | Which phase(s) to run. Accepts `dump_and_load`, `dump_only`, `load_only`. |
| `STAGED_DUMP_DIR` | `"${RUN_DIR}/dumps"` | Directory for dump artifacts. For `load_only`, point at the directory containing `manifest.txt`. |
| `STAGED_COMPRESS` | `"1"` | gzip-compress dump artifacts. Set to `"0"` for raw `.sql` files. |
| `STAGED_PV` | `"1"` | Show live progress via `pv` (or a 60s file-size probe fallback). Set to `"0"` to silence. |
| `STAGED_PARALLEL` | `"4"` | Concurrent per-DB dump workers. Set to `"1"` for sequential. |
| `STAGED_LOAD_PARALLEL` | `"1"` | Concurrent per-DB load workers. Default sequential; target write throughput is usually the bottleneck. |
| `STAGED_VERIFY_CHECKSUM` | `"1"` | Verify per-file integrity using a SHA-256 checksum against the manifest before loading. Deprecated alias: `STAGED_VERIFY_SHA256`. |
| `STAGED_DISK_HEADROOM_FACTOR` | `"2"` | Multiplier on source data size for the dump-volume free-space pre-flight check. |
| `STAGED_FINALIZE_VARIANCE_PCT` | `"50"` | Row-count variance threshold (between manifest and target) above which finalize emits a warning. Both numbers are InnoDB sampled estimates. Deprecated alias: `STAGED_FINALIZE_DRIFT_PCT`. |
| `STAGED_CONFIRM_OFFLINE` | `""` | Set to any non-empty value to bypass the interactive offline-acknowledgment prompt (useful in CI). |

## Deprecated / hidden modes

The following modes and their variables are present in the codebase but not exposed via the interactive prompts or `--help`. They may be removed in a future release.

### Target install (deprecated)

As of v1.1.0-beta, MariaDB must be pre-installed on the target host before running the migration tool. The install code path remains for backward compatibility but defaults to `"0"` (skip) and emits a deprecation warning if explicitly set to `"1"`. The install scripts will be removed in v2.0.

Variables (deprecated):

| Name | Old default | Description |
|---|---|---|
| `INSTALL_TARGET_MARIADB` | now `"0"` (was `"1"`) | Whether to install MariaDB on the target. Default flipped to skip in v1.1.0-beta. |
| `TARGET_INSTALL_OS` | `"ubuntu"` | OS family for the target install. |
| `TARGET_MARIADB_VERSION` | `"11.8"` | MariaDB version to install on the target. |
| `INSTALL_CONFIGURE_BIND_ADDRESS` | `"1"` | Whether the install step should configure MariaDB's bind-address. |
| `INSTALL_MARIADB_BIND_ADDRESS` | `"0.0.0.0"` | Bind address used when the install step configures it. |
| `INSTALL_AUTO_GRANT_TARGET_ADMIN` | `"1"` | Whether the install step should auto-grant privileges to the target admin user. |
| `INSTALL_TARGET_ADMIN_HOST_PATTERN` | `"%"` | Host pattern used for the auto-grant. |

For users who previously relied on the tool to install MariaDB on the target, the recommended approach is to follow the standard MariaDB installation guide for your OS, then re-run the migration tool against the now-running target.

### Replace-slave (hidden)

Variables prefixed `REPLACE_*` configure the experimental replace-slave mode. Not documented here; see `scripts/01_replace_slave_*.sh` if you need details.

### In-place upgrade

Variables prefixed `INPLACE_*` configure the in-place MySQL-to-MariaDB upgrade mode. Used internally; see `scripts/02_inplace_*.sh` for current behavior.

---

## Deprecated aliases

These variable names are honored for backward compatibility but emit a deprecation notice. Use the new names in new configurations.

| Old name | New name | Removed in |
|---|---|---|
| `STAGED_VERIFY_SHA256` | `STAGED_VERIFY_CHECKSUM` | TBD |
| `STAGED_FINALIZE_DRIFT_PCT` | `STAGED_FINALIZE_VARIANCE_PCT` | TBD |

---

## Examples

### Minimal `staged` `dump_and_load` config

```yaml
mode: "staged"
env:
  SRC_HOST: "mysql.internal"
  SRC_ADMIN_USER: "admin"
  SRC_ADMIN_PASS: "..."
  SRC_DBS: "sakila,world"
  TGT_HOST: "mariadb.internal"
  TGT_ADMIN_USER: "admin"
  TGT_ADMIN_PASS: "..."
```

### `staged` `load_only` against MariaDB Cloud

```yaml
mode: "staged"
env:
  STAGED_PHASE: "load_only"
  STAGED_DUMP_DIR: "/mnt/transfer/dumps"
  TGT_HOST: "dbxxx.skysql.com"
  TGT_PORT: "3306"
  TGT_ADMIN_USER: "admin"
  TGT_ADMIN_PASS: "..."
```

> The connection to MariaDB Cloud is encrypted in transit; server-certificate verification is not yet configurable. See the "Target connection" note above.

### `staged` `dump_only` from AWS RDS source

```yaml
mode: "staged"
env:
  STAGED_PHASE: "dump_only"
  STAGED_DUMP_DIR: "/mnt/transfer/dumps"
  SRC_HOST: "database-1.xxx.region.rds.amazonaws.com"
  SRC_ADMIN_USER: "admin"
  SRC_ADMIN_PASS: "..."
  SRC_DBS: "sakila"
  SRC_SSL_MODE: "REQUIRED"
```
