/*
schema_default_encryption.sql

Tables encrypted by inheritance from a database created with
DEFAULT ENCRYPTION='Y', as opposed to tables that carry the attribute
themselves.

The two behave differently on MariaDB and must be told apart:

  explicit   ENCRYPTION='Y' is in the table's CREATE_OPTIONS, reaches the
             dump as DDL, and fails the load with 1911. Caught by
             load_fail_objects.sql.
  inherited  nothing in the table's DDL, nothing in the dump. The database
             loads, the clause is gone, and every table in it is unencrypted
             with no error anywhere. That silence is why it is a LOAD_FAIL
             rather than a sign-off item.

INFORMATION_SCHEMA.SCHEMATA_EXTENSIONS is NOT usable for this: on 8.4.7 a
database created with DEFAULT ENCRYPTION='Y' still reports an empty OPTIONS,
indistinguishable from a database with no encryption at all. The encrypted
state is only visible per tablespace.

So: tablespaces marked encrypted whose table does not carry the attribute
itself were encrypted by inheritance. Grouped by schema, since the finding is
about the database's default rather than any one table.

Needs PROCESS, like the other INNODB_* checks. INNODB_TABLESPACES is the 8.0
name (5.7 calls it INNODB_SYS_TABLESPACES and has no ENCRYPTION column), so
the view is probed first; on 5.7 this returns empty, which is correct enough
-- 5.7 has no DEFAULT ENCRYPTION clause to inherit from.

Columns:  1 obj_schema  2 encrypted_table_count
*/

SET @has_enc := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'INNODB_TABLESPACES'
    AND COLUMN_NAME  = 'ENCRYPTION'
);

SET @sql := IF(@has_enc > 0,
  'SELECT SUBSTRING_INDEX(s.NAME, ''/'', 1) AS obj_schema,
          COUNT(*) AS encrypted_tables
     FROM information_schema.INNODB_TABLESPACES s
     JOIN information_schema.TABLES t
       ON t.TABLE_SCHEMA = SUBSTRING_INDEX(s.NAME, ''/'', 1)
      AND t.TABLE_NAME   = SUBSTRING_INDEX(SUBSTRING_INDEX(s.NAME, ''/'', -1), ''#'', 1)
    WHERE s.ENCRYPTION = ''Y''
      AND s.NAME LIKE ''%/%''
      AND COALESCE(t.CREATE_OPTIONS, '''') NOT LIKE ''%ENCRYPTION=''''Y''''%''
      AND SUBSTRING_INDEX(s.NAME, ''/'', 1) NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    GROUP BY 1
    ORDER BY 1',
  'SELECT NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
