/*
schema_default_encryption.sql

Databases created with DEFAULT ENCRYPTION='Y'. MariaDB skips the clause
inside its version comment, so the database loads with its encryption
default gone and tables created in it afterwards arrive unencrypted.

This is the silent half of database encryption. The loud half is each
existing table: a table that inherited encryption still carries a bare
ENCRYPTION='Y' in its own DDL, identical to an explicitly encrypted table,
so it reaches the dump and fails the load with 1911. load_fail_objects.sql
catches those. What is left here is the database default itself, including
for a database with no tables yet, which no per-table check can see.

INFORMATION_SCHEMA.SCHEMATA.DEFAULT_ENCRYPTION reports the setting directly
(8.0.16+) and needs no PROCESS, unlike the INNODB_* checks. The column is
probed first; on 5.7 this returns empty, which is correct -- 5.7 has no
DEFAULT ENCRYPTION clause. SCHEMATA_EXTENSIONS.OPTIONS is NOT usable: it is
empty on 8.0.46 and 8.4.7 whether or not the database is encrypted.

A source running with default_table_encryption=ON reports YES for every
database created while it was set, so the finding can be broad. The remedy
is the same either way: on MariaDB encryption is a server-level setting,
not a database attribute.

Columns:  1 obj_schema  2 base_table_count
*/

SET @has_col := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'SCHEMATA'
    AND COLUMN_NAME  = 'DEFAULT_ENCRYPTION'
);

SET @sql := IF(@has_col > 0,
  'SELECT s.SCHEMA_NAME AS obj_schema,
          CONCAT(COUNT(t.TABLE_NAME), '' existing table(s)'') AS base_table_count
     FROM information_schema.SCHEMATA s
     LEFT JOIN information_schema.TABLES t
       ON t.TABLE_SCHEMA = s.SCHEMA_NAME
      AND t.TABLE_TYPE   = ''BASE TABLE''
    WHERE s.DEFAULT_ENCRYPTION = ''YES''
      AND s.SCHEMA_NAME NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    GROUP BY 1
    ORDER BY 1',
  'SELECT NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
