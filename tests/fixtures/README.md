# Gap zoo

`gap_zoo.sql` creates one object per known-risk feature, so a migration can be
tested end to end and each check confirmed against what actually happens.

Expected classification is in a comment above each object: `LOAD_FAIL`,
`SIGN_OFF` or `INFO`, matching the `class` field in `report.json` and the
section it should land in within `load_failure_report.txt`.

## Running it

```bash
mysql --force < gap_zoo.sql          # --force: one failure should not stop the rest
mariadb-migrator --assess            # read load_failure_report.txt
mariadb-migrator                     # migrate, and see whether the prediction held
```

A disagreement between the report and the migration is the interesting result.
It means a check is wrong, not the test — that is how the `_0900_` split was
settled (see `collation_support.sql`).

## Setup this file cannot do on its own

Three objects need more than DDL. Without the setup below those statements
fail on MySQL and the corresponding checks go untested.

**Encryption (`z_table_encrypted`, and the schema-level case).** MySQL needs a
keyring component loaded before `ENCRYPTION='Y'` is accepted. With
`component_keyring_file`, create `$MYSQL_HOME/component_keyring_file.cnf`
pointing at a writable data file, add the component to
`mysqld.my`, restart, and confirm with
`SELECT * FROM performance_schema.keyring_component_status;`.

The schema-level case is a separate statement, deliberately not in the main
file because it creates a second database:

```sql
CREATE DATABASE gap_zoo_enc DEFAULT ENCRYPTION='Y';
CREATE TABLE gap_zoo_enc.inherits_encryption (id INT PRIMARY KEY);
```

Expected: `LOAD_FAIL`. MariaDB drops the clause silently and new tables in that
database land unencrypted — no error, which is why it is classed as a failure
rather than a sign-off item.

**General tablespace (`z_tablespace_general`).** Create the tablespace first:

```sql
CREATE TABLESPACE zoo_ts1 ADD DATAFILE 'zoo_ts1.ibd' ENGINE=InnoDB;
```

Reading it back during assessment needs `PROCESS` on the source. Without that
privilege `information_schema.INNODB_TABLESPACES` returns empty rather than
erroring, and the check would report clean on a source that has tablespaces —
the assessment warns separately when it detects this. Worth running the
assessment once as a restricted user to confirm that warning fires.

**Generated invisible primary key (`z_invisible_generated_pk`).** Needs
`SET sql_generate_invisible_primary_key = ON;` in the session before the
`CREATE TABLE`, or `sql_generate_invisible_primary_key=ON` in the server
config (8.0.30+).

**Routine-level collation.** A procedure carries the session's
`collation_connection` from creation time, and the dump restores it before
`CREATE PROCEDURE`, so an unsupported collation fails the load even when no
column uses it:

```sql
SET collation_connection = 'utf8mb4_ja_0900_as_cs';
CREATE PROCEDURE gap_zoo.z_routine_collation() BEGIN SELECT 1; END;
SET collation_connection = DEFAULT;
```

Expected: `LOAD_FAIL` (1273), reported against the routine rather than a
column.

## Confirmed on 8.4.7

`AUTOEXTEND_SIZE` does not appear in `TABLES.CREATE_OPTIONS` — that column is
empty for such tables. The value is on
`information_schema.INNODB_TABLESPACES.AUTOEXTEND_SIZE`, which is what the
check reads. `SHOW CREATE TABLE` shows it inside a `/*!80023 ... */` version
comment, which MariaDB skips, hence `SIGN_OFF` rather than a load failure.

Likewise a table's general tablespace is not visible in `CREATE_OPTIONS`;
it takes a join from `INNODB_TABLES` to `INNODB_TABLESPACES`.

`SCHEMATA_EXTENSIONS.OPTIONS` is empty for a database created with
`DEFAULT ENCRYPTION='Y'` — identical to a database with no encryption, so it
cannot be used to detect the case. The only visible evidence is
`INNODB_TABLESPACES.ENCRYPTION = 'Y'` on the tables that inherited it. A table
encrypted by inheritance is distinguished from one carrying the attribute
itself by the absence of `ENCRYPTION='Y'` in its own `CREATE_OPTIONS`; the
first is silent on MariaDB, the second fails with 1911.

## How findings are classified

Whether a construct actually breaks depends on whether it reaches the target's
parser, and that is decided by the dump rather than by either server. Reading
the dump settles it:

```bash
mariadb-dump -h <src> -u <user> -p --ssl-verify-server-cert=OFF --no-data \
  --databases gap_zoo gap_zoo_enc > /tmp/gap_zoo_schema.sql
grep -nE "INVISIBLE|NOT ENFORCED|AUTOEXTEND|TABLESPACE|SRID|ENCRYPTION" \
  /tmp/gap_zoo_schema.sql
```

Confirmed against MySQL 8.4.7:

| Construct | In the dump | Consequence |
|---|---|---|
| `AUTOEXTEND_SIZE` | `/*!80023 */` | skipped, dropped |
| `NOT ENFORCED` | `/*!80016 */` | skipped; constraint arrives enforced |
| column `INVISIBLE` | `/*!80023 */` | skipped, column visible (MDEV-41696) |
| index `INVISIBLE` | `/*!80000 */` | skipped, index visible |
| `SRID` | `/*!80003 */` | skipped, restriction gone |
| `TABLESPACE` | `/*!50100 */` | skipped, file-per-table |
| database `DEFAULT ENCRYPTION` | `/*!80016 */` | skipped; future tables unencrypted |
| table `ENCRYPTION='Y'` | **bare** | reaches parser, fails 1911 |

MariaDB skips version comments numbered 50700-99999 (MDEV-5009), so everything
wrapped is dropped on every MariaDB version regardless of what that version
supports. Probing those would mislead: `AUTOEXTEND_SIZE` parses on 12.3+, yet
the dump hides it there too.

A table that inherits encryption from its database gets a **bare**
`ENCRYPTION='Y'` written into its own DDL, so it fails with 1911 like any
other encrypted table. Only the database's own clause is silently dropped,
which affects tables created after the migration.

Constructs that do reach the parser -- routine and view bodies, collations,
character sets -- are probed against the target at assess time rather than
assumed. Support varies by build: `ST_IsValid` resolves on 12.3.3 and 13.1.1
but not 11.8.9; `->>` parses on 13.1.1 but not 11.8.9 or 12.3.3.
`collation_support.sql` and the probe set in `orchestrator/probes.py` are how
those answers were obtained.

## Known gaps

Not yet represented, to add as the related tickets land: reserved words,
functional indexes, roles, resource groups, partial revokes.

Unicode weight differences are not covered either. MySQL's `_0900_` collations
use UCA 9.0.0 and MariaDB's `uca1400` aliases use 14.0.0, so strings that
compare equal on one may not on the other. On a unique index that changes which
rows collide — a load can fail on a duplicate key for data MySQL accepted, or
succeed while uniqueness quietly means something different. A case for that
would need a table whose unique key holds strings that differ between the two
weight tables.
