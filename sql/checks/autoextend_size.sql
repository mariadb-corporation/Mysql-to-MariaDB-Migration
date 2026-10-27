/*
autoextend_size.sql

AUTOEXTEND_SIZE on a table (8.0.23+). MariaDB does not recognise it; the
attribute is dropped and tablespace extension reverts to the MariaDB default.
Loads without error -- the dump carries it inside a /*!80023 ... *''/ version
comment, which MariaDB skips.

NOT in TABLES.CREATE_OPTIONS, which is empty for these tables (confirmed on
8.4.7). The value lives on INNODB_TABLESPACES.AUTOEXTEND_SIZE, zero when
unset, so that is what is queried here.

Reading INNODB_TABLESPACES needs PROCESS; without it the view returns empty
rather than erroring, which checks.py warns about separately.

INNODB_TABLESPACES.NAME is 'db/table' for file-per-table spaces.
AUTOEXTEND_SIZE arrived in 8.0.23, so the column is probed first.

Columns:  1 obj_schema  2 table_name  3 autoextend_size
*/

SET @has_aes := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'INNODB_TABLESPACES'
    AND COLUMN_NAME  = 'AUTOEXTEND_SIZE'
);

SET @sql := IF(@has_aes > 0,
  'SELECT SUBSTRING_INDEX(NAME, ''/'', 1) AS obj_schema,
          SUBSTRING_INDEX(SUBSTRING_INDEX(NAME, ''/'', -1), ''#'', 1) AS table_name,
          AUTOEXTEND_SIZE
     FROM information_schema.INNODB_TABLESPACES
    WHERE AUTOEXTEND_SIZE > 0
      AND NAME LIKE ''%/%''
      AND SUBSTRING_INDEX(NAME, ''/'', 1) NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY 1, 2',
  'SELECT NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
