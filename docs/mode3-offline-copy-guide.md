# Offline Copy (`staged`) mode: prerequisites, monitoring, and troubleshooting

What has to be true before an Offline Copy run, how the tool moves schema and
data through on-disk dump files, how to watch a run while it is in flight, and
what to do when it breaks.

This is the fully **offline** mode. The source must be quiesced — writes stopped
— for the dump to be consistent, and the tool asks you to acknowledge that
before it starts. There is no live catch-up phase. If you need near-zero
downtime, use Replication instead. The two streaming modes (`one_step`,
`two_step`) also copy while offline but move data over the wire; Offline Copy is
the one to reach for when source and target cannot reach each other on the
network, or when you want a durable, inspectable artifact and a checkpoint
between producing the data and loading it.

## How it works

Two operations against on-disk files, driven by `mariadb-dump` on the way out
and the `mariadb` client on the way in:

1. **Dump** — `mariadb-dump` writes one file per database to the dump directory,
   and a manifest sidecar records what was produced (databases, file names, and
   SHA-256 checksums).
2. **Load** — the `mariadb` client replays each dump file into the target.
3. **Analyze** — once the load completes, an `ANALYZE TABLE` phase refreshes the
   target's optimizer statistics (opt-out; see below). This runs before
   verification, not after it.
4. **Finalize** — a finalize step verifies the target against the manifest.

Which of those two you run is controlled by `STAGED_PHASE`:

- **`dump_and_load`** (default) — dump and load in one continuous run.
- **`dump_only`** — produce the dump artifacts and stop. Load them later, or on
  a different host.
- **`load_only`** — load from dump artifacts you already have. Point
  `STAGED_DUMP_DIR` at the manifest directory; in interactive runs the tool
  auto-detects the most recent `artifacts/run_staged_*/dumps/` directory as the
  default.

Splitting dump from load is the whole point of the mode: you can dump from a
source behind a restrictive network, move the files, and load into the target on
your own schedule.

### What "offline" means here — and what it does not

- **The dump is only consistent if the source is not being written to.** The
  tool does not lock the source or coordinate a consistent snapshot for you. It
  asks — "stopped writes to source?" — and defaults to `n`, so pressing Enter
  does **not** silently bypass the acknowledgment. Set `STAGED_CONFIRM_OFFLINE`
  to answer it up front in a non-interactive run.
- **The load is not per-database resumable.** If a multi-database load fails
  partway through, re-running reloads every database — it does not pick up where
  it stopped. The recovery model is explicit: drop the partially-loaded
  databases on the target and re-run with `STAGED_PHASE=load_only`. (Contrast
  with Parallel Restartable Streaming Copy, whose "restartable" refers to
  in-session auto-retry — a different mechanism that does not apply here.)

## Prerequisites

Each item below has a check you can run before the migration and, where the
preflight enforces it, the gate it corresponds to.

**Source MySQL** (required for `dump_and_load` and `dump_only`):

- **A `mariadb-dump` that can read the source.** Confirm the binary and that it
  connects:
  ```
  mariadb-dump --version
  mariadb-dump -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_ADMIN_USER" -p \
    --no-data --databases sakila | head
  ```
  For a MySQL 8.4 source, use an upstream MySQL 8.4 `mysqldump` and point
  `MARIADB_DUMP_BIN` at it — the MariaDB-packaged dump client can trip on 8.4
  server metadata. Preflight warns when the source major/minor version is ahead
  of the dump client.

- **Admin credentials with read on the target databases.** `SRC_ADMIN_USER` /
  `SRC_ADMIN_PASS` must be able to `SELECT` the schemas named in `SRC_DB` /
  `SRC_DBS`. Verify:
  ```
  mysql -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_ADMIN_USER" -p \
    -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='sakila';"
  ```

- **Disk headroom on the dump volume.** Preflight queries the source data size
  from `information_schema`, checks free space on the dump volume with `df`, and
  warns when free space is below `STAGED_DISK_HEADROOM_FACTOR × source_size`
  (default `2×`). Check it yourself:
  ```
  df -h "$STAGED_DUMP_DIR"
  ```

**Target MariaDB** (required for `dump_and_load` and `load_only`):

- **A reachable target and admin credentials.** `TGT_ADMIN_USER` /
  `TGT_ADMIN_PASS` must be able to create databases and load data:
  ```
  mysql -h "$TGT_HOST" -P "$TGT_PORT" -u "$TGT_ADMIN_USER" -p -e "SELECT VERSION();"
  ```
  Note `localhost` uses the Unix socket and ignores the port; use `127.0.0.1`
  when you mean a specific TCP port.

- **On managed targets (RDS, MariaDB Cloud), no reliance on global toggles.**
  The load path does not set `GLOBAL FOREIGN_KEY_CHECKS`; foreign-key ordering is
  handled within the dump/load. Nothing to prepare here — just don't expect to
  set server globals on a managed instance.

**Load host** (when dumping and loading from a third machine):

- Running the tool from a host that is neither source nor target needs only
  TCP reachability to both. The load connects to the target over the network;
  `TGT_SSH_HOST` is not required (it applies to the deprecated install path
  and to `replace_slave` mode only). MariaDB must already be installed and
  running on the target — the launcher does not install it.

## Engine notes and gotchas

- **A load can exit 0 even when statements failed.** The `mariadb` client
  continues past a failed statement by default and still exits 0 at end of input,
  and a shell pipeline reports only the last command's exit code — so
  `mariadb-dump | mariadb` can return success while tables or rows were silently
  dropped. Don't treat the exit code as the gate; the finalize step is what
  actually confirms the load (see below).

- **Checksums guard the artifacts.** With `STAGED_VERIFY_SHA256=1` (default) each
  dump file is checked against the manifest's SHA-256 before loading, so a
  truncated or corrupted transfer between hosts is caught before it reaches the
  target rather than after.

- **Compression is on by default.** `STAGED_COMPRESS=1` gzips the per-database
  dump files. Turn it off (`0`) only if you are moving to a load host that will
  benefit from uncompressed input or you are short on CPU rather than disk.

- **`--parallel` alone buys nothing.** `mariadb-dump --parallel` without a
  directory-format target (`--dir`) delivers no speedup, and `--dir` requires
  server-side `SELECT ... INTO OUTFILE`, which is blocked or awkward on managed
  and hardened hosts (`secure_file_priv`, SELinux, file-ownership between client
  and server). Today's parallelism is at the **per-database** level via a job
  pool, not intra-table. Directory-format parallel dump/import
  (`STAGED_FORMAT=dir`) is planned but not the current default.

## The dump directory and manifest

The dump directory is where the mode's durable state lives — treat it the way
you would a backup.

- **Location.** Defaults to `${RUN_DIR}/dumps`. Override with `STAGED_DUMP_DIR`.
  For `load_only`, `STAGED_DUMP_DIR` is required and must point at the directory
  that contains the manifest and the per-database files.
- **Contents.** One dump file per database (`.sql`, or `.sql.gz` when compressed)
  plus a manifest sidecar recording the databases, file names, and SHA-256
  checksums. The finalize step reads the manifest to verify the load.
- **Moving artifacts between hosts.** Copy the whole directory — files *and*
  manifest — as a unit. `load_only` needs the manifest to know what to load and
  to run the checksum check; a directory of `.sql.gz` files without its manifest
  is not loadable by the tool.

## Post-load ANALYZE TABLE

A dump/restore leaves the target's optimizer statistics empty or stale, which
can produce poor query plans until something refreshes them. After the load
completes, Offline Copy runs an `ANALYZE TABLE` phase across the base tables of
each migrated database so the MariaDB optimizer has accurate statistics
immediately — no waiting for a background refresh or a first slow query to
trigger one.

- **It is a prompt, defaulting to on.** During setup, right after the "Migrate
  application users?" prompt, you are asked "Run ANALYZE TABLE on target after
  load? (y/n)" with a default of `y`. Answer `n`, or set `ANALYZE_TARGET=0`, to
  skip it.
- **Where it runs in the sequence.** After the load, before finalize
  verification — so the statistics are in place by the time the run reports
  success.
- **It self-skips on `dump_only`.** No load happens on a dump-only host, so
  there is nothing to analyze; the phase is skipped automatically. It is active
  for `dump_and_load` and `load_only`.
- **Per-table errors are non-fatal.** An `ANALYZE TABLE` failure on an individual
  table is reported but does not fail the migration; only a failure to connect
  to the target is fatal. A report is written to
  `artifacts/run_staged_<ts>/analyze_target_report.txt` listing the tables
  analyzed, status counts, and any per-table errors.

## The finalize step

Finalize is the last thing an Offline Copy run does, and it is the real success
gate — the check that catches the silent partial-load an exit code of 0 can
hide. It runs after the load (and after ANALYZE) and reads the manifest to
confirm that what was dumped actually landed on the target.

What it checks, per database in the manifest:

- **Presence.** The database exists on the target. A database that is in the
  manifest but missing on the target is a **hard failure** — the run reports
  failure.
- **Non-emptiness.** The database has tables. If the manifest recorded data for
  a database but the target shows zero tables for it, that is a **hard
  failure** — this is exactly the case a "successful" load with dropped DDL
  produces.
- **Row-count drift (informational).** Finalize also compares row counts between
  source and target and prints a **warning** — never a failure — when a database
  differs by more than `STAGED_FINALIZE_DRIFT_PCT` (default 50%). A drift warning
  does **not** mean the migration failed or that rows were lost. It is a
  heads-up to look closer, for two reasons: (1) the row counts it compares come
  from `information_schema.tables`, whose `TABLE_ROWS` figure is an *estimate*
  for InnoDB — derived from optimizer statistics, not an exact count, and often
  off by a wide margin on both sides; and (2) some real divergence is expected
  and fine (a table excluded by a filter, an engine difference). Because it is
  report-only, it flags for review and lets the run finish.

Finalize does its counting against the target's `information_schema.tables`, so
it is checking the target's own view of what it holds, not re-reading the dump.
Read its output before you trust a run: the completion banner alone is not
confirmation — the finalize lines printed just above the banner are where a
missing or short database shows up.

### Reconfirming a drift warning

Because the finalize comparison uses estimates, the way to settle whether a
flagged database actually lost rows is to take **exact** counts on both sides
and compare them. `SELECT COUNT(*)` does a real scan and returns the true
number, unlike the `information_schema` estimate.

Per table — authoritative, and what to reach for on the specific tables finalize
flagged:
```
-- on the source
mysql -h "$SRC_HOST" -P "$SRC_PORT" -u "$SRC_ADMIN_USER" -p \
  -e "SELECT COUNT(*) FROM sakila.rental;"
-- on the target
mysql -h "$TGT_HOST" -P "$TGT_PORT" -u "$TGT_ADMIN_USER" -p \
  -e "SELECT COUNT(*) FROM sakila.rental;"
```

For a whole database at once, generate one `COUNT(*)` per table and total them.
Run this on each side and diff the two outputs:
```
mysql -N -h "$HOST" -P "$PORT" -u "$USER" -p -e "
  SELECT CONCAT('SELECT \"', table_name, '\", COUNT(*) FROM \`sakila\`.\`',
                table_name, '\`;')
  FROM information_schema.tables
  WHERE table_schema = 'sakila' AND table_type = 'BASE TABLE';" \
| mysql -N -h "$HOST" -P "$PORT" -u "$USER" -p
```
If the per-table exact counts match between source and target, the load is
complete and the drift warning was just estimate noise — nothing to act on. If
they genuinely differ, treat it as a real gap: the recovery model is the same as
any incomplete load — drop the affected database on the target and re-run with
`STAGED_PHASE=load_only`.

> Refreshing the estimate instead of counting: running `ANALYZE TABLE` updates
> `information_schema`'s row estimate, so re-checking after an analyze will often
> make a spurious drift warning disappear. But `ANALYZE TABLE` still only
> produces an estimate — for a definitive answer, use `SELECT COUNT(*)`.

## Monitoring a run

- **Watch the run log.** Every long-running phase prints the exact command to
  follow progress from another terminal:
  ```
  tail -f "$RUN_DIR/run.log"
  ```
- **Progress meter.** With `STAGED_PV=1` (default) the dump and load pipelines
  show a `pv` meter; because the orchestrator captures output to a pipe rather
  than a TTY, `pv` runs with `-f -i 10` so a line still lands in `run.log` every
  ten seconds. On hosts without `pv` installed, a `stat`-based file-size probe
  emits a per-database line every 60 seconds with bytes written, elapsed time,
  rate, and percent complete.
- **Completion banners tell you what to do next.** A `dump_only` run ends with a
  "DUMP COMPLETE" banner naming the manifest and printing the exact `load_only`
  command to run later; `load_only` ends with "LOAD COMPLETE"; `dump_and_load`
  uses the standard "MIGRATION SUCCESSFUL" banner. In all cases the finalize
  output above the banner is the thing to actually read.

## `max_allowed_packet`

A single row larger than the target's `max_allowed_packet` stops the load with
`ERROR 2006 (Server has gone away)`. The row cannot be split — one row is one
statement, so there is no seam to break at. MySQL 8.x defaults to 64 MB and
MariaDB to 16 MB, so a source row between those two sizes will transfer cleanly
from the source and fail on the target.

Before the run phase the tool compares the limits on both ends. When the target
has at least twice the source's limit no scan runs. Otherwise the tool scans
tables holding TEXT, BLOB, or JSON columns, reports any oversized rows, and
offers to raise the limit on the target:

    Raise max_allowed_packet on the target to 67108864 now? (y/n) [y]:

Answering `y` issues `SET GLOBAL max_allowed_packet`, which applies to new
connections only and does not survive a restart. To persist it, set the value
under `[mysqld]` on the target and restart:

    max_allowed_packet=67108864

Answering `n` allows the run to continue. There is no second check later, so a
run that declines will fail at the load if an oversized row is reached.

## Troubleshooting

- **"Target admin user and password are required" on `dump_only`.** `dump_only`
  never touches a target and should not ask for target credentials. If it does,
  the phase gate on target-credential validation is being hit — confirm
  `STAGED_PHASE=dump_only` is actually set in the environment the run sees.

- **`STAGED_DUMP_DIR is not set and RUN_DIR is not set`.** Seen when invoking the
  orchestrator directly (`python3 -m orchestrator.migrationctl run`) rather than
  through the launcher: `RUN_DIR` was not exported to the phase subprocess. Set
  `STAGED_DUMP_DIR` explicitly (or run through the launcher, which exports
  `RUN_DIR`).

- **Load "succeeded" but tables are missing or short.** Trust finalize over the
  exit code. Finalize hard-fails if a database in the manifest is absent or has
  zero tables on the target, and warns when row counts drift more than
  `STAGED_FINALIZE_DRIFT_PCT` (default 50%). If finalize flags a database, drop
  it on the target and re-run `STAGED_PHASE=load_only`.

- **A multi-database load failed partway.** There is no per-database resume. Drop
  the databases that were partially loaded and re-run with
  `STAGED_PHASE=load_only` pointed at the same `STAGED_DUMP_DIR`. The already-good
  databases are re-loaded too — that is expected.

- **Checksum mismatch on `load_only`.** The dump file does not match the manifest
  — usually a truncated or partial copy between hosts. Re-copy the dump directory
  as a whole and retry; do not disable `STAGED_VERIFY_SHA256` to work around it.

- **Disk-headroom warning at preflight.** Free space on the dump volume is below
  `STAGED_DISK_HEADROOM_FACTOR × source`. Point `STAGED_DUMP_DIR` at a larger
  volume, leave `STAGED_COMPRESS=1`, or lower the factor only if you have
  measured the compressed footprint and know it fits.

## Worked example

Concrete values: source MySQL at `mysql-src` (`192.168.1.20`), target MariaDB at
`mariadb-tgt`, admin user `migadmin`, database `sakila`. Adjust to your
environment.

### Interactive

Run the launcher from the repo root, choose **Assess & Plan** then **Run**, pick
mode **3) Offline Copy**, and answer the sub-phase prompt (`dump_and_load` for a
single continuous run). When it asks whether writes to the source are stopped,
answer `y` only once they actually are.

### Non-interactive (prompt-free)

Save the run configuration once, then invoke the orchestrator directly. The
`STAGED_*` and `SRC_*` / `TGT_*` values live in `config/migration.yaml` — they
are read from the config, not from exported shell variables.

`config/migration.yaml`:
```yaml
MODE: staged
STAGED_PHASE: dump_and_load
STAGED_CONFIRM_OFFLINE: "1"      # source writes are already stopped

SRC_HOST: 192.168.1.20
SRC_PORT: 3306
SRC_ADMIN_USER: migadmin
SRC_ADMIN_PASS: "Migration101$"
SRC_DBS: sakila

TGT_HOST: mariadb-tgt
TGT_PORT: 3306
TGT_ADMIN_USER: migadmin
TGT_ADMIN_PASS: "Migration101$"
INSTALL_TARGET_MARIADB: 0

STAGED_DUMP_DIR: /data/migration/dumps
STAGED_PARALLEL: 4
```

Run it:
```
cd /path/to/Mysql-to-MariaDB-Migration
python3 -m orchestrator.migrationctl run
tail -f "$RUN_DIR/run.log"      # in another terminal
```

### Dump now, load later

Two runs against the same dump directory. First, on or near the source:
```yaml
MODE: staged
STAGED_PHASE: dump_only
STAGED_CONFIRM_OFFLINE: "1"
SRC_HOST: 192.168.1.20
SRC_PORT: 3306
SRC_ADMIN_USER: migadmin
SRC_ADMIN_PASS: "Migration101$"
SRC_DBS: sakila
STAGED_DUMP_DIR: /data/migration/dumps
```
```
python3 -m orchestrator.migrationctl run
```
Copy `/data/migration/dumps` — files and manifest together — to the load host,
then load:
```yaml
MODE: staged
STAGED_PHASE: load_only
TGT_HOST: mariadb-tgt
TGT_PORT: 3306
TGT_ADMIN_USER: migadmin
TGT_ADMIN_PASS: "Migration101$"
STAGED_DUMP_DIR: /data/migration/dumps
```
```
python3 -m orchestrator.migrationctl run
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `STAGED_PHASE` | `dump_and_load` | Phase selector: `dump_and_load`, `dump_only`, `load_only` |
| `STAGED_DUMP_DIR` | `${RUN_DIR}/dumps` | Dump file location; required for `load_only` |
| `STAGED_COMPRESS` | `1` | Gzip-compress dump files |
| `STAGED_PV` | `1` | Progress meter (`pv` if available, else file-size probe) |
| `STAGED_PARALLEL` | `4` | Concurrent per-database dumps |
| `STAGED_LOAD_PARALLEL` | `1` | Concurrent per-database loads (sequential by default) |
| `STAGED_VERIFY_SHA256` | `1` | Verify each dump file's SHA-256 against the manifest before load |
| `STAGED_DISK_HEADROOM_FACTOR` | `2` | Multiplier on source size for the dump-volume free-space check |
| `STAGED_FINALIZE_DRIFT_PCT` | `50` | Row-count drift threshold above which finalize warns |
| `STAGED_CONFIRM_OFFLINE` | unset | Set to bypass the interactive offline-acknowledgment prompt |
| `ANALYZE_TARGET` | `1` | Run `ANALYZE TABLE` on the target after load; `0` to skip (auto-skipped on `dump_only`) |
| `MARIADB_DUMP_BIN` | auto | Path to the dump client; point at upstream MySQL 8.4 `mysqldump` for 8.4 sources |
