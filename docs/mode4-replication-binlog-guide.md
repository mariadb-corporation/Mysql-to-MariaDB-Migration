# Replication (binlog) mode: prerequisites, status checks, and troubleshooting

Replication is the tool's only **online** mode. It seeds the target from a
consistent snapshot, then keeps it current from the source's binary log until
you cut over. Everything below is what has to be true for that to work, the
concrete commands to set it up and check it, and the failures seen in testing.

The tool (`scripts/14_binlog_seed.sh` → `15_binlog_start_replication.sh` →
`16_binlog_verify.sh`) automates the seed and the replication start. The
`my.cnf`, replication user, and connectivity setup are yours to do regardless;
the by-hand seed and `CHANGE MASTER` steps are documented here so you can run —
or recover — the process manually.

Example values used throughout (replace with your own):

| | Value |
|---|---|
| Source (MySQL) | host `mysql-src`, `server_id = 1` |
| Target / replica (MariaDB) | `192.168.1.20` (hostname `mariadb-tgt`), `server_id = 2` |
| Replication user | `repl_user` / `Migration101$` |
| Admin / dump user | `migadmin` |
| Database being migrated | `sakila` |

## How it works

Two phases stitched together:

1. **Seed** — a consistent snapshot of the source is taken at a known binlog
   coordinate and loaded into the target. The target now matches the source as
   of that exact position.
2. **Replicate** — the target is pointed at the source as a replica, starting
   from that coordinate, and plays forward every change made after the snapshot.

Replication mode does **not** use `mariadb-mtk`. The data path is `mariadb-dump`
for the seed, then native MySQL→MariaDB binary-log replication.

## Prerequisites

### 1. Source (MySQL) `my.cnf`

```ini
[mysqld]
server-id                       = 1
log-bin                         = /var/log/mysql/mysql-bin.log
binlog_format                   = ROW
log_bin_trust_function_creators = 1
mysql_native_password           = ON          # plugin must be available for the repl user
bind-address                    = 0.0.0.0     # reachable from the target
```

Restart MySQL, then confirm binary logging is active:

```sql
SHOW VARIABLES LIKE 'log_bin';        -- expect: ON
SHOW VARIABLES LIKE 'binlog_format';  -- expect: ROW
SHOW BINARY LOGS;                     -- lists the binlog files
SHOW BINARY LOG STATUS;               -- MySQL 8.4+ (SHOW MASTER STATUS on 8.0): current File + Position
```

### 2. Target (MariaDB) `my.cnf`

```ini
[mysqld]
server_id     = 2            # must be non-zero AND different from the source
log_bin       = mariadb-bin
binlog_format = ROW
local_infile  = 1
```

Restart MariaDB. A `server_id` of `0`, or one that matches the source, will make
`CHANGE MASTER TO` fail.

### 3. Replication user (on the source)

The replica connects **from** the target, so the user must exist for the address
the source sees. Create it for every form the target might present — wildcard,
IP, and hostname — and use `mysql_native_password`.

```sql
CREATE USER IF NOT EXISTS 'repl_user'@'%'            IDENTIFIED WITH 'mysql_native_password' BY 'Migration101$';
CREATE USER IF NOT EXISTS 'repl_user'@'192.168.1.20' IDENTIFIED WITH 'mysql_native_password' BY 'Migration101$';
CREATE USER IF NOT EXISTS 'repl_user'@'mariadb-tgt'  IDENTIFIED WITH 'mysql_native_password' BY 'Migration101$';

GRANT REPLICATION SLAVE ON *.* TO 'repl_user'@'%';
GRANT REPLICATION SLAVE ON *.* TO 'repl_user'@'192.168.1.20';
GRANT REPLICATION SLAVE ON *.* TO 'repl_user'@'mariadb-tgt';
FLUSH PRIVILEGES;

SELECT user, host FROM mysql.user WHERE user = 'repl_user';   -- verify all three
```

### 4. Connectivity from the target to the source

Before configuring replication, confirm the target can actually log in to the
source as the replication user. This is the precondition for `START SLAVE`:

```bash
# run on the MariaDB target
mariadb -h mysql-src -u repl_user -p --ssl=0
```

### 5. No JSON columns in the migrated databases

**Hard requirement.** MySQL's native JSON type does not survive MySQL→MariaDB
binary-log replication under any realistic workload (see Troubleshooting for the
exact failures). This must return zero rows:

```sql
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
FROM information_schema.COLUMNS
WHERE DATA_TYPE = 'json'
  AND TABLE_SCHEMA IN ('sakila');
```

If it returns any rows, use an offline mode (Serial Streaming, Parallel
Restartable Streaming, or Offline Staged). The preflight blocks Replication mode
when JSON columns are present.

### Also required

- **Source MySQL 8.0 or later.** Replication mode is not supported from pre-8.0
  sources; the preflight routes them to an offline mode.
- **Binlog retention long enough to cover seed + catch-up.** If the source
  purges the binlog the snapshot was anchored to before the replica reaches it,
  replication breaks. Check and raise if needed:
  ```sql
  SHOW VARIABLES LIKE 'binlog_expire_logs_seconds';
  SET GLOBAL binlog_expire_logs_seconds = 86400;   -- 24h floor
  ```
- **Network reachability** from the orchestrator host to both ends, and from the
  target to the source (that link outlives the run).
- **Correct dump binary for the source.** For an 8.4 source, set
  `MARIADB_DUMP_BIN` to an upstream MySQL 8.4 `mysqldump` — `mariadb-dump` cannot
  read 8.4 server metadata.

## Running it by hand (what the tool automates)

The tool embeds the coordinate with `mariadb-dump --source-data=2`
(`--master-data=2` on pre-8.4 sources) and parses it into `binlog_coords.env`.
The steps below are the manual equivalent — useful for recovery or to see
exactly what happens.

### Capture the coordinate, then seed the target

Read the current coordinate under a brief lock:

```sql
-- on the source; note the File and Position
FLUSH TABLES WITH READ LOCK;
SHOW BINARY LOG STATUS;        -- e.g.  mysql-bin.000005 | 1455
UNLOCK TABLES;
```

Then seed the target. Prepend `SET SQL_LOG_BIN=0` so the restore is not itself
written to the target's binlog; `--single-transaction` gives a consistent
snapshot; `--gtid=0` keeps it file+position based:

```bash
# run on the target
( echo "SET SQL_LOG_BIN=0;"; \
  mariadb-dump -h mysql-src -u migadmin -p'<password>' \
    --databases sakila --routines --triggers \
    --gtid=0 --no-tablespaces --hex-blob --single-transaction --skip-ssl ) \
  | pv -petb | mariadb -u migadmin -p'<password>'
```

### Point the target at the source and start

```sql
-- on the MariaDB target
STOP SLAVE;
RESET SLAVE;
CHANGE MASTER TO
  MASTER_HOST     = 'mysql-src',
  MASTER_USER     = 'repl_user',
  MASTER_PASSWORD = 'Migration101$',
  MASTER_LOG_FILE = 'mysql-bin.000005',   -- File from SHOW BINARY LOG STATUS
  MASTER_LOG_POS  = 1455,                  -- Position
  MASTER_SSL      = 0,                     -- set 1, and drop the line below, if the source requires TLS
  MASTER_SSL_VERIFY_SERVER_CERT = 0;
START SLAVE;
SHOW SLAVE STATUS\G
```

## Checking replication status

Run on the **MariaDB target** (the replica):

```sql
SHOW SLAVE STATUS\G      -- SHOW REPLICA STATUS\G also works on MariaDB 10.5+
```

The fields that matter:

| Field | Healthy value | Meaning |
|---|---|---|
| `Slave_IO_Running` | `Yes` | Replica is reading binlogs from the source |
| `Slave_SQL_Running` | `Yes` | Replica is applying them |
| `Seconds_Behind_Master` | `0`, or trending down | Lag; `NULL` means a thread has stopped |
| `Read_Master_Log_Pos` vs `Exec_Master_Log_Pos` | converging | Received position vs applied position |
| `Master_Log_File` / `Relay_Master_Log_File` | same, advancing | Which source binlog is being read |
| `Master_Server_Id` | the source's `server_id` | Confirms it's connected to the right source |
| `Using_Gtid` | `No` | File+position based, as seeded |
| `Last_IO_Error` / `Last_SQL_Error` | empty | Last error on each thread |

Both threads `Yes` with `Seconds_Behind_Master` at or near `0` means the target
is caught up and ready to cut over.

Quick end-to-end check — write on the source, read on the target:

```sql
-- source (MySQL)
CREATE TABLE sakila.repl_check (id INT PRIMARY KEY, msg VARCHAR(64));
INSERT INTO sakila.repl_check VALUES (1, 'replication alive');

-- target (MariaDB)
SELECT * FROM sakila.repl_check;
```

On the source you can also see connected replicas and the current position:

```sql
SHOW BINARY LOG STATUS;   -- MySQL 8.4+ (SHOW MASTER STATUS on 8.0)
SHOW REPLICAS;            -- 8.4+ (SHOW SLAVE HOSTS on 8.0)
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[ERROR] In RBR mode, Slave received unknown table event` | MySQL 8.0.2+ writes extra column metadata (`binlog_row_metadata`) that MariaDB's SQL thread can't parse | On the source set `binlog_row_metadata = MINIMAL` (and `binlog_row_value_options = ""`), restart, re-seed. This is the fix for **non-JSON** ROW replication. |
| `[ERROR] In RBR mode, Slave received unknown field type field 245 for column …activity_data` | The value is a native MySQL **JSON** column (type 245); MariaDB's replication layer does not understand it | No fix — JSON is not supported over MySQL→MariaDB replication. Use an offline mode. Preflight should have blocked this; check whether a JSON column was added after preflight. |
| Replica stops (`Slave_SQL_Running: No`) after a stored procedure or bulk job | `binlog_format` is not `ROW` | Set `binlog_format = ROW` on the source, as the prerequisites require. ROW is the only supported format for this migration path. |
| `Slave_IO_Running: Connecting`, generic access-denied for `repl_user` | Wrong host in the grant, or the `mysql_native_password` plugin not active | Recreate the user `IDENTIFIED WITH 'mysql_native_password'` for the target's `%`/IP/hostname. Re-check with the step-4 login. |
| Was replicating, then `Slave_IO_Running: No` with "could not find first log file" / purged-log error | The source purged the binlog the seed was anchored to before catch-up finished | Retention too short. Raise `binlog_expire_logs_seconds`, then re-seed from a fresh snapshot. |
| Duplicate-key or missing rows right after `START SLAVE` | Seed coordinate and start position don't line up | Re-seed; the `MASTER_LOG_FILE`/`POS` must come from the same snapshot that was loaded. Don't hand-edit the coordinate. |
| `CHANGE MASTER TO` refused / replica won't start | Target `server_id = 0`, or source and target share a `server_id` | Set a unique non-zero `server_id` on the target (in `my.cnf` and `SET GLOBAL server_id=…`). |
| Seed fails reading source metadata (8.4 source) | `mariadb-dump` used against an 8.4 server | Set `MARIADB_DUMP_BIN` to an upstream MySQL 8.4 `mysqldump`. |
| One specific statement fails and stops the SQL thread, but you know it's safe to drop | e.g. a DDL/grant that has no effect on the target | Skip exactly one event: `STOP SLAVE; SET GLOBAL SQL_SLAVE_SKIP_COUNTER = 1; START SLAVE;` — this **discards** that change, so only use it for statements you're sure are safe to lose. |

## Recovering / resetting

Stop and fully forget the replica configuration before a clean retry:

```sql
STOP SLAVE;
RESET SLAVE ALL;
```

Then re-seed and re-run `CHANGE MASTER TO` from a fresh coordinate.

## Related

- `docs/run-files-config-and-cleanup.md` — config files, run artifacts layout,
  and cleanup (including `binlog_coords.env`).
